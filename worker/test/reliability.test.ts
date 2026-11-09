import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import pg from 'pg';
import { Artifact, SendMessageRequest, Task, TaskState } from '@a2a-js/sdk';
import { DefaultExecutionEventBus, DefaultRequestHandler, ServerCallContext, type RequestContext } from '@a2a-js/sdk/server';
import { WorkerStore } from '../src/store.js';
import { PiExecutor } from '../src/executor.js';
import { createPiWorker } from '../src/pi-runtime.js';
import { card, createWorkerServer } from '../src/server.js';

const enabled = !!process.env.REXAK_TEST_DATABASE_URL;
const pool = new pg.Pool({ connectionString: process.env.REXAK_TEST_DATABASE_URL, max: 6 });
after(async () => { await pool.end(); });
const context = new ServerCallContext({ requestedVersion: '1.0', user: { isAuthenticated: true, userName: 'rexak-control' } });
function gate() { let open!: () => void; const promise = new Promise<void>(r => { open = r; }); return { promise, open }; }
async function fixture() {
  const store = new WorkerStore(pool);
  const { record } = await store.reserve(randomUUID(), 'test-digest', { objective: 'private-marker-UNIQUE', context_refs: [] });
  const task = (await store.load(record.task_id))!;
  const bus = new DefaultExecutionEventBus();
  const events: string[] = [];
  bus.on('event', event => { events.push(event.kind); });
  const ctx = { taskId: task.id, contextId: task.contextId } as RequestContext;
  return { store, record, task, bus, events, ctx };
}

test('provider failure stays FAILED after controller reconciliation and retains its reason', { skip: !enabled }, async () => {
  const f = await fixture();
  const executor = new PiExecutor(f.store, { fail: true });
  await executor.execute(f.ctx, f.bus);
  const result = (await f.store.load(f.task.id))!;
  assert.equal(result.status!.state, TaskState.TASK_STATE_FAILED);
  assert.equal((await f.store.byTask(f.task.id))!.cancel_requested, false);
  const artifact = Artifact.toJSON(result.artifacts[0]) as any;
  assert.match(JSON.stringify(artifact), /Injected mock provider failure/);
});

test('shared configuration failure records FAILED without starting Pi or leaking keys', { skip: !enabled }, async () => {
  const f = await fixture();
  let created = 0;
  const executor = new PiExecutor(f.store, {}, (...args) => { created++; return createPiWorker(...args); }, undefined,
    async () => { throw new Error('fixture-private-model-key'); });
  await executor.execute(f.ctx, f.bus);
  const task = (await f.store.load(f.task.id))!;
  assert.equal(created, 0);
  assert.equal(task.status!.state, TaskState.TASK_STATE_FAILED);
  assert.ok(!JSON.stringify(Task.toJSON(task)).includes('fixture-private-model-key'));
});

test('task resolves current configuration before Pi creation and retains it for its calls', { skip: !enabled }, async () => {
  const f = await fixture();
  const config = { kind: 'openai-compatible' as const, provider: 'bailian' as const, model: 'fixture-model',
    base_url: 'https://fixture.invalid/compatible-mode/v1', api_key: 'fixture-new-key',
    temperature: 1, max_tokens: 10, context_window: 100, timeout_ms: 1000 };
  let received = false;
  const executor = new PiExecutor(f.store, {}, (objective, options) => {
    assert.equal(options?.modelConfig, config);
    received = true;
    // Exercise real Pi's deterministic Mock loop; never send a provider request.
    return createPiWorker(objective, { ...options, modelConfig: undefined, callHooks: undefined });
  }, undefined, async id => { assert.equal(id, f.record.request_id); return config; });
  await executor.execute(f.ctx, f.bus);
  assert.ok(received);
  assert.equal((await f.store.load(f.task.id))!.status!.state, TaskState.TASK_STATE_COMPLETED);
  assert.ok(!JSON.stringify(Task.toJSON((await f.store.load(f.task.id))!)).includes('fixture-new-key'));
});

test('cancel after claim waits for execution and prevents all Pi/model/tool startup', { skip: !enabled }, async () => {
  const f = await fixture(), claimed = gate(), release = gate();
  const original = f.store.byTask.bind(f.store);
  let once = false, created = 0;
  f.store.byTask = async id => {
    const row = await original(id);
    if (row?.phase === 'running' && !once) { once = true; claimed.open(); await release.promise; }
    return row;
  };
  const executor = new PiExecutor(f.store, {}, (...args) => { created++; return createPiWorker(...args); });
  const running = executor.execute(f.ctx, f.bus);
  await claimed.promise;
  let confirmed = false;
  const cancelled = executor.confirmCancellation(f.task.id).then(value => { confirmed = true; return value; });
  await new Promise(resolve => setTimeout(resolve, 25));
  assert.equal(confirmed, false);
  assert.ok(![3, 4, 5].includes((await f.store.load(f.task.id))!.status!.state));
  release.open();
  await running;
  assert.equal((await cancelled)!.status!.state, TaskState.TASK_STATE_CANCELED);
  assert.equal(created, 0);
  assert.deepEqual((await f.store.load(f.task.id))!.artifacts[0].metadata!.event_types, []);
});

test('SDK working-save failure cannot terminate a blocked Pi; executor result wins', { skip: !enabled }, async () => {
  const f = await fixture(), entered = gate(), release = gate(), failedSave = gate();
  const executor = new PiExecutor(f.store, {}, (...args) => {
    const worker = createPiWorker(...args);
    return { ...worker, run: async () => { entered.open(); await release.promise; return worker.run(); } };
  });
  const save = f.store.save.bind(f.store);
  let injected = false;
  f.store.save = async (task, ctx) => {
    if (!injected && task.status?.state === TaskState.TASK_STATE_WORKING) { injected = true; failedSave.open(); throw new Error('Injected transient WORKING save failure'); }
    return save(task, ctx);
  };
  const handler = new DefaultRequestHandler(card('http://127.0.0.1:8795'), f.store, executor);
  await handler.sendMessage(SendMessageRequest.fromJSON({ message: { messageId: f.record.request_id, taskId: f.task.id, contextId: f.task.contextId, role: 'ROLE_USER', parts: [{ data: f.record.payload, mediaType: 'application/json' }] }, configuration: { returnImmediately: true } }), context);
  await Promise.all([entered.promise, failedSave.promise]);
  // Wait for the SDK's error drain to attempt its synthesized FAILED write.
  for (let i = 0; i < 100; i++) {
    if ((await f.store.load(f.task.id))?.metadata?.protocol_diagnostic) break;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  const pending = (await f.store.load(f.task.id))!;
  assert.equal(pending.metadata!.protocol_diagnostic, 'sdk_terminal_event_ignored');
  assert.ok(![3, 4, 5].includes(pending.status!.state));
  assert.equal((await f.store.byTask(f.task.id))!.phase, 'running');
  release.open();
  for (let i = 0; i < 100; i++) {
    if ((await f.store.load(f.task.id))!.status!.state === TaskState.TASK_STATE_COMPLETED) break;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  const finished = (await f.store.load(f.task.id))!;
  assert.equal(finished.status!.state, TaskState.TASK_STATE_COMPLETED);
  assert.equal(finished.artifacts.length, 1);
  assert.equal((await f.store.byTask(f.task.id))!.phase, 'finished');
});

test('atomic recovery rejects both old executor completion and SDK snapshot writes', { skip: !enabled }, async () => {
  const f = await fixture(), entered = gate(), release = gate();
  const executor = new PiExecutor(f.store, {}, (...args) => {
    const worker = createPiWorker(...args);
    return { ...worker, run: async () => { entered.open(); await release.promise; return worker.run(); } };
  });
  const running = executor.execute(f.ctx, f.bus);
  const rejected = assert.rejects(running, /claim is no longer current/);
  await entered.promise;
  const replacement = new WorkerStore(pool);
  await replacement.recoverInterrupted();
  const late = Task.fromJSON(Task.toJSON(f.task));
  late.status!.state = TaskState.TASK_STATE_COMPLETED;
  late.artifacts = [Artifact.fromJSON({ artifactId: randomUUID(), parts: [{ data: { summary: 'late forged result' } }] })];
  await f.store.save(late, context);
  release.open();
  await rejected;
  assert.equal((await replacement.byTask(f.task.id))!.phase, 'interrupted');
  const task = (await replacement.load(f.task.id))!;
  assert.equal(task.status!.state, TaskState.TASK_STATE_UNSPECIFIED);
  assert.equal(task.artifacts.length, 0);
  assert.ok(!f.events.includes('artifactUpdate'));
  assert.equal(await new PiExecutor(replacement).confirmCancellation(f.task.id), undefined);
});

test('lost advisory-lock connection fences writes and rejects new requests', { skip: !enabled }, async () => {
  const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_TEST_DATABASE_URL!, token: 'mock-controlled-token-at-least-24', baseUrl: 'http://127.0.0.1:8795' });
  const server = runtime.app.listen(0, '127.0.0.1');
  try {
    const rows = await pool.query("SELECT pid FROM pg_locks WHERE locktype='advisory' AND classid=8795 AND objid=1 AND database=(SELECT oid FROM pg_database WHERE datname=current_database())");
    assert.equal(rows.rowCount, 1);
    await pool.query('SELECT pg_terminate_backend($1)', [rows.rows[0].pid]);
    for (let i = 0; i < 100 && runtime.store.available; i++) await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(runtime.store.available, false);
    const address = server.address() as { port: number };
    const response = await fetch(`http://127.0.0.1:${address.port}/health`);
    assert.equal(response.status, 503);
    await assert.rejects(runtime.store.reserve(randomUUID(), 'x', {}), /ownership lost/);
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()));
    // Lock connection was deliberately terminated; close must still release its pool.
    await runtime.close();
  }
});
