import { test } from 'node:test';
import assert from 'node:assert/strict';
import { remoteTools } from '../src/remote-tools.js';
import { createPiWorker } from '../src/pi-runtime.js';
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, createFindToolDefinition } from '../vendor/pi-tools/index.js';
const channel = { requestId: 'request', taskId: 'task', workerId: 'worker', contextId: 'context', token: 'fixture-only-token', controlUrl: 'http://127.0.0.1:8770', ensureActive: async () => {} };

test('official factories use only injected POSIX Operations and retain exact-edit conflict detection', async () => {
  let content = 'Alpha\n';
  const paths: string[] = [];
  const operations = {
    access: async (p: string) => { paths.push(p); },
    readFile: async (p: string) => { paths.push(p); return Buffer.from(content); },
    writeFile: async (p: string, data: string) => { paths.push(p); content = data; },
    mkdir: async (p: string) => { paths.push(p); },
    exists: async () => true,
    glob: async () => ['note.txt'],
    detectImageMimeType: async () => null,
  };
  const root = '/kali/session/.rexak/workers/one';
  await createWriteToolDefinition(root, { operations }).execute('write', { path: 'note.txt', content: 'Alpha\n' });
  const read = await createReadToolDefinition(root, { operations }).execute('read', { path: 'note.txt' });
  assert.match(JSON.stringify(read), /Alpha/);
  const edit = createEditToolDefinition(root, { operations });
  await edit.execute('edit', { path: 'note.txt', edits: [{ oldText: 'Alpha', newText: 'Beta' }] });
  assert.equal(content, 'Beta\n');
  await assert.rejects(edit.execute('conflict', { path: 'note.txt', edits: [{ oldText: 'Missing', newText: 'Gamma' }] }));
  assert.ok(paths.every(p => p.startsWith('/kali/')));
  const found = await createFindToolDefinition(root, { operations }).execute('find', { pattern: '*.txt' });
  assert.match(JSON.stringify(found), /note.txt/);
});

test('six remote tools expose official schemas without synonyms', () => {
  assert.deepEqual(remoteTools(channel, () => {}).tools.map(t => t.name).sort(), ['bash', 'edit', 'find', 'grep', 'read', 'write']);
});

test('remote find preserves backend truncation even below the official result count limit', async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    const request = JSON.parse(String(init?.body));
    const value = request.phase === 'begin' ? { workspace: '/kali/worker' }
      : request.phase === 'step' ? { ok: true, data: request.arguments.action === 'exists' ? { exists: true }
        : { lines: ['note.txt'], truncated: true } } : { saved: true };
    return Response.json(value);
  };
  try {
    const tool = remoteTools(channel, () => {}).tools.find(t => t.name === 'find')!;
    const result = await tool.execute('find-short', { pattern: '*.txt' });
    assert.equal((result.details as any).executor_output_truncated, true);
  } finally { globalThis.fetch = previous; }
});

for (const mode of ['lost-response', 'malformed-response', 'server-failed']) {
  test(`${mode} latches unknown and prevents a second effect`, async () => {
    const previous = globalThis.fetch;
    let sends = 0, aborted = false;
    globalThis.fetch = async () => {
      sends++;
      if (mode === 'lost-response') throw new Error('lost');
      if (mode === 'malformed-response') return new Response('{');
      return new Response('failed', { status: 503 });
    };
    try {
      const remote = remoteTools(channel, () => { aborted = true; });
      const bash = remote.tools.find(t => t.name === 'bash')!;
      await assert.rejects(bash.execute('one', { command: 'printf safe' }));
      await assert.rejects(bash.execute('two', { command: 'printf safe' }));
      assert.equal(sends, 1); assert.ok(aborted && remote.uncertain);
    } finally { globalThis.fetch = previous; }
  });
}

test('running ToolCall ID deduplicates identical arguments but rejects tool/argument conflicts', async () => {
  const previous = globalThis.fetch;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let steps = 0;
  globalThis.fetch = async (_url, init) => {
    const request = JSON.parse(String(init?.body));
    if (request.phase === 'begin') { await gate; return Response.json({ workspace: '/kali/one' }); }
    if (request.phase === 'step') { steps++; return Response.json({ data: { output: 'done', exit_code: 0, truncated: false }, output_refs: [] }); }
    return Response.json({ saved: true });
  };
  try {
    const tools = remoteTools(channel, () => {}).tools;
    const bash = tools.find(t => t.name === 'bash')!;
    const pending = bash.execute('same', { command: 'printf one' });
    const replay = bash.execute('same', { command: 'printf one' });
    await assert.rejects(bash.execute('same', { command: 'printf different' }), /conflicts/);
    await assert.rejects(tools.find(t => t.name === 'read')!.execute('same', { path: 'file' }), /conflicts/);
    release();
    assert.deepEqual(await pending, await replay);
    assert.equal(steps, 1);
  } finally { release(); globalThis.fetch = previous; }
});

test('provider failure requests failure cleanup without user-cancel provenance', async () => {
  const previous = globalThis.fetch;
  let cleanup: any;
  globalThis.fetch = async (_url, init) => { cleanup = JSON.parse(String(init?.body)); return Response.json({ settled: true }); };
  try {
    const worker = createPiWorker('synthetic provider failure', { fail: true, evidence: channel,
      task: { handoff_version: 1, context_id: 'context', context_sources: [], context_refs: [], objective: 'failure', background: '', completion_requirements: [], execution_access: 'pi_tools_v1' } });
    await assert.rejects(worker.run());
    assert.equal(cleanup.phase, 'settle'); assert.equal(cleanup.arguments.cancel, true);
    assert.equal(cleanup.arguments.reason, 'failure');
    assert.equal(worker.executionUncertain, false);
  } finally { globalThis.fetch = previous; }
});
