import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import pg from 'pg';
import { Artifact, TaskState } from '@a2a-js/sdk';
import { snapshot, restore } from '../src/history.js';
import { createPiWorker } from '../src/pi-runtime.js';
import { WorkerStore } from '../src/store.js';
import { PiExecutor } from '../src/executor.js';
import { steeringInbox } from '../src/collaboration.js';

const channel = { requestId: 'request', taskId: 'task', workerId: 'worker', contextId: 'context', token: 'fixture-only-token', controlUrl: 'http://127.0.0.1:8770', ensureActive: async () => {} };
const task = { handoff_version: 1 as const, context_id: 'context', context_sources: [], context_refs: [], objective: 'fixture', execution_access: 'pi_tools_v1' as const };
function canonical(v: any): string {
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
  return JSON.stringify(v);
}
const hash = (v: any) => createHash('sha256').update(canonical(v)).digest('hex');
async function history() { const worker = createPiWorker('normal'); await worker.run(); return snapshot(worker.agent.state.messages); }

test('P1-1 new nested HTTP credentials never enter the successor physical model request; legacy v1 rejects safely', async () => {
  const clean = await history();
  const markers = ['TEST_COOKIE', 'TEST_BASIC', 'TEST_BEARER', 'TEST_PROXY', 'TEST_QUOTED', 'TEST_SET'];
  const headers = 'Cookie: first=a; sid=TEST_COOKIE; quoted="TEST_QUOTED"\nAuthorization: Basic TEST_BASIC\nProxy-Authorization: Bearer TEST_PROXY\nSet-Cookie: session=TEST_SET; Secure; HttpOnly\nAuthorization: Bearer TEST_BEARER\nKEEP_FLAG{route_cookie=value}\nordinary technical route';
  const raw: any[] = structuredClone(clean.messages);
  raw.find(m => m.role === 'user').content = [{ type: 'text', text: headers }];
  raw.find(m => m.role === 'assistant').content.find((p: any) => p.type === 'toolCall').arguments = { nested: { command: `curl -H 'Cookie: sid=TEST_COOKIE; x="TEST_QUOTED"'`, headers: { 'Proxy-Authorization': 'Basic TEST_PROXY' } } };
  raw.find(m => m.role === 'toolResult').content = [{ type: 'text', text: headers }];
  const saved = snapshot(raw);
  for (const marker of markers) assert.ok(!JSON.stringify(saved).includes(marker));
  const restored = restore(saved, saved.sha256);
  const legacy = { version: 1, messages: raw };
  assert.throws(() => restore(legacy, hash(raw)), /unsupported fields/);
  // Old partial masking from the reviewed v1 code is unsafe as well.
  const oldPartial = structuredClone(clean.messages) as any[];
  oldPartial.find(m => m.role === 'user').content = 'Cookie: [REDACTED]; sid=TEST_COOKIE\nAuthorization: [REDACTED] TEST_BASIC';
  assert.throws(() => restore({ version: 1, messages: oldPartial }, hash(oldPartial)), /unsupported fields/);
  const previous = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async (_url, init) => {
    requests++;
    const body = String(init?.body);
    for (const marker of markers) assert.ok(!body.includes(marker));
    assert.ok(body.includes('KEEP_FLAG{route_cookie=value}') && body.includes('ordinary technical route'));
    return new Response('data: {"id":"safe","choices":[{"index":0,"delta":{"role":"assistant","content":"KEEP_FLAG{route_cookie=value}"},"finish_reason":null}]}\n\ndata: {"id":"safe","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
  };
  try {
    const worker = createPiWorker('continue', { history: restored, modelConfig: { kind: 'openai-compatible', provider: 'bailian', model: 'fixture', base_url: 'https://fixture.example/v1', api_key: 'fixture-only', temperature: 1, max_tokens: 2000, context_window: 32000, timeout_ms: 1000 } });
    assert.match(await worker.run(), /KEEP_FLAG/); assert.equal(requests, 1);
  } finally { globalThis.fetch = previous; }
});

for (const status of [403, 409]) {
  test(`P1-3 begin ${status} with no leaves and confirmed cleanup is a definite failure`, async () => {
    const previous = globalThis.fetch; const phases: string[] = [];
    globalThis.fetch = async (_url, init) => {
      const request = JSON.parse(String(init?.body)); phases.push(request.phase);
      return request.phase === 'begin' ? new Response('denied', { status }) : Response.json({ settled: true });
    };
    try {
      const worker = createPiWorker('denied', { task, evidence: channel });
      await assert.rejects(worker.run());
      assert.deepEqual(phases, ['begin', 'settle']);
      assert.equal(worker.executionUncertain, false);
    } finally { globalThis.fetch = previous; }
  });
}

test('P1-3 local admission failure has no step; accepted step response loss stays unknown without re-post', async () => {
  const previous = globalThis.fetch; let steps = 0;
  globalThis.fetch = async (_url, init) => {
    const req = JSON.parse(String(init?.body));
    if (req.phase === 'begin') return Response.json({ workspace: '/fixture' });
    if (req.phase === 'step') { steps++; throw new Error('response lost after acceptance'); }
    return Response.json({ settled: false });
  };
  try {
    const worker = createPiWorker('lost', { task, evidence: channel });
    await assert.rejects(worker.run(), /unresolved/);
    assert.equal(worker.executionUncertain, true); assert.equal(steps, 1);
    let phases: string[] = [];
    globalThis.fetch = async (_url, init) => { phases.push(JSON.parse(String(init?.body)).phase); return Response.json({ settled: true }); };
    const denied = createPiWorker('local denied', { task, evidence: { ...channel, ensureActive: async () => { throw new Error('control revoked'); } } });
    await assert.rejects(denied.run()); assert.equal(denied.executionUncertain, false); assert.deepEqual(phases, ['settle']);
  } finally { globalThis.fetch = previous; }
});

const enabled = !!process.env.REXAK_TEST_DATABASE_URL;
test('P1-2 NUL history cannot roll back a real completed task; DB faults still propagate', { skip: !enabled }, async () => {
  const pool = new pg.Pool({ connectionString: process.env.REXAK_TEST_DATABASE_URL });
  try {
    const store = new WorkerStore(pool); const source = randomUUID(), session = randomUUID();
    const r = await store.reserve(source, 'nul', { objective: 'nul', context_refs: [], session_id: session, context_id: randomUUID() });
    await store.claim(r.record.task_id);
    const saved = await history(); const raw: any[] = structuredClone(saved.messages);
    raw.find(m => m.role === 'toolResult').content = [{ type: 'text', text: 'actual tool output\u0000tail' }];
    const result = Artifact.fromJSON({ artifactId: randomUUID(), parts: [{ data: { summary: 'valid final answer' } }] });
    const completed = await store.complete(r.record.task_id, TaskState.TASK_STATE_COMPLETED, result, raw);
    assert.equal(completed.status?.state, TaskState.TASK_STATE_COMPLETED);
    assert.equal(completed.metadata?.history_available, false);
    assert.equal((await store.request(source))?.phase, 'finished');
    assert.equal((await store.load(r.record.task_id))?.artifacts[0].artifactId, result.artifactId);
    await assert.rejects(store.history(source, session), /unavailable/);
    const successor = await store.reserve(randomUUID(), 'resume-nul', { objective: 'continue', context_refs: [], session_id: session, context_id: randomUUID(), resume_from_delegation_id: source });
    let factoryCalled = false;
    const executor = new PiExecutor(store, {}, (() => { factoryCalled = true; throw new Error('must not start'); }) as any);
    await executor.execute({ taskId: successor.record.task_id, contextId: successor.record.payload.context_id } as any, { publish: () => {}, finished: () => {} } as any);
    assert.equal(factoryCalled, false);
    assert.equal((await store.load(successor.record.task_id))?.status?.state, TaskState.TASK_STATE_FAILED);
    // A real database schema failure during INSERT is not mistaken for malformed history.
    const next = await store.reserve(randomUUID(), 'db-failure', { objective: 'db', context_refs: [] }); await store.claim(next.record.task_id);
    const conn = await pool.connect();
    await conn.query('ALTER TABLE ultra_worker.histories RENAME TO histories_fault_fixture');
    try { await assert.rejects(store.complete(next.record.task_id, TaskState.TASK_STATE_COMPLETED, result, saved.messages), (error: any) => error.code === '42P01'); }
    finally { await conn.query('ALTER TABLE ultra_worker.histories_fault_fixture RENAME TO histories'); conn.release(); }
    assert.equal((await store.byTask(next.record.task_id))?.phase, 'running'); // DB failure must stay visible.
  } finally { await pool.end(); }
});

test('P1-5 delayed first admission blocks the next Inbox insert; native Pi consumes [1,2] once', { skip: !enabled }, async () => {
  const pool = new pg.Pool({ connectionString: process.env.REXAK_TEST_DATABASE_URL });
  const previous = globalThis.fetch;
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  let arrived!: () => void; const admission = new Promise<void>(resolve => { arrived = resolve; });
  try {
    const store = new WorkerStore(pool); const context = randomUUID();
    const { record } = await store.reserve(randomUUID(), 'inbox-order', { objective: 'live', context_refs: [], context_id: context, collaboration_access: 'collaboration_v1' });
    await store.claim(record.task_id);
    const msg = (sequence: number) => ({ id: randomUUID(), sequence, task_id: record.task_id, worker_id: record.worker_id, context_id: context, message: `clue-${sequence}`, context_sources: [], trust: 'untrusted_task_context_no_authority_change' });
    const first = msg(1), second = msg(2); const steered: number[] = [], consumed: number[] = [];
    const worker = createPiWorker('live');
    const inbox = steeringInbox(worker.agent, async () => {}, async id => { consumed.push(id === first.id ? 1 : id === second.id ? 2 : 3); await store.messageStatus(id, 'consumed'); });
    const executor = new PiExecutor(store, {}, undefined, { controlUrl: channel.controlUrl, token: channel.token });
    (executor as any).active.set(record.task_id, { cancelled: false, abort: () => {}, done: Promise.resolve(), steer: (message: any) => { steered.push(message.sequence); inbox(message); } });
    globalThis.fetch = async url => { if (String(url).includes(first.id)) { arrived(); await gate; } return Response.json({ allowed: true }); };
    const pending = executor.enqueueMessage(record.request_id, first, 'first'); await admission;
    assert.equal((await store.message(record.request_id, first.id)).status, 'enqueuing');
    await assert.rejects(executor.enqueueMessage(record.request_id, second, 'second'), /Previous message/);
    assert.deepEqual(steered, []); release(); await pending;
    await executor.enqueueMessage(record.request_id, first, 'first'); // Replay returns receipt; no second steer.
    await executor.enqueueMessage(record.request_id, second, 'second');
    assert.deepEqual(steered, [1, 2]); await worker.run(); assert.deepEqual(consumed, [1, 2]);
    const third = msg(3); const originalStatus = store.messageStatus.bind(store);
    store.messageStatus = async (id, status) => { if (status === 'queued') throw new Error('queue acknowledgement lost'); await originalStatus(id, status); };
    await assert.rejects(executor.enqueueMessage(record.request_id, third, 'third'), /acknowledgement lost/);
    assert.equal((await store.message(record.request_id, third.id)).status, 'enqueuing');
    store.messageStatus = originalStatus;
    await executor.enqueueMessage(record.request_id, third, 'third');
    await worker.run();
    assert.deepEqual(steered, [1, 2, 3]); assert.deepEqual(consumed, [1, 2, 3]);
    await store.cancelRequest(record.request_id);
    await assert.rejects(executor.enqueueMessage(record.request_id, msg(4), 'late'), /unavailable/);
    assert.deepEqual(steered, [1, 2, 3]);
  } finally { release(); globalThis.fetch = previous; await pool.end(); }
});
