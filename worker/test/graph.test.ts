import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHmac } from 'node:crypto';
import { graphTools } from '../src/graph.js';
import { createPiWorker } from '../src/pi-runtime.js';
import { taskPrompt, type TaskPackage } from '../src/task-package.js';

test('graph grant does not expand retained task background or completed history', () => {
  const base: TaskPackage = { objective: 'Synthetic task', context_refs: [], background: 'Existing background' };
  const granted: TaskPackage = { ...base, graph_access: 'session_graph_read_v1' };
  assert.equal(taskPrompt(granted), taskPrompt(base));
  assert.equal(granted.graph_access, 'session_graph_read_v1');
});

test('graph tools enforce fixed runtime identity, read-only actions, cancellation and errors', async () => {
  const received: any[] = [];
  const server = createServer((req, res) => {
    let raw = ''; req.on('data', c => { raw += c; }); req.on('end', () => {
      received.push({ body: JSON.parse(raw), auth: req.headers.authorization });
      res.end(JSON.stringify({ revision: 7, total: 0, records: [] }));
    });
  });
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  try {
    const channel = { controlUrl: `http://127.0.0.1:${(server.address() as any).port}`, token: 'runtime-secret',
      requestId: 'd', taskId: 't', workerId: 'w', contextId: 'c', ensureActive: async () => {} };
    const graph = graphTools(channel);
    assert.deepEqual(graph.tools.map(t => t.name), ['graph_overview', 'graph_query', 'read_shared_graph']);
    await graph.tools[1].execute('call', { query: { ids: ['hidden'] }, task_id: 'foreign', action: 'update_graph' } as any);
    assert.equal(received[0].body.action, 'query'); assert.equal(received[0].body.task_id, 't');
    assert.equal(received[0].auth, 'Bearer ' + createHmac('sha256', channel.token).update('graph:v1:d:t:w:c').digest('hex'));
    channel.ensureActive = async () => { throw new Error('cancelled'); };
    await assert.rejects(graph.tools[0].execute('cancelled', {} as any), /cancelled/);
    assert.equal(received.length, 1);
  } finally { await new Promise<void>(r => server.close(() => r())); }
});

test('graph initial read failure leaves native Pi execution available and dynamic graph out of fixed system', async () => {
  const server = createServer((_req, res) => { res.statusCode = 409; res.end('{}'); });
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  try {
    const worker = createPiWorker('Synthetic task', { task: { objective: 'Synthetic task', context_refs: [], graph_access: 'session_graph_read_v1' },
      evidence: { controlUrl: `http://127.0.0.1:${(server.address() as any).port}`, token: 'runtime-secret', requestId: 'd', taskId: 't', workerId: 'w', contextId: 'c', ensureActive: async () => {} } });
    const system = worker.agent.state.systemPrompt;
    await worker.run();
    assert.equal(worker.observations.length, 2);
    assert.equal(worker.agent.state.systemPrompt, system);
    assert.ok(!worker.agent.state.messages.some(m => m.role === 'user' && String(m.content).startsWith('当前 Session 共享双图')));
  } finally { await new Promise<void>(r => server.close(() => r())); }
});

test('native cancellation interrupts an in-flight initial graph read', async () => {
  let reached: () => void = () => {};
  const reading = new Promise<void>(resolve => { reached = resolve; });
  const server = createServer((_req, _res) => { reached(); });
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  try {
    const worker = createPiWorker('Synthetic task', { task: { objective: 'Synthetic task', context_refs: [], graph_access: 'session_graph_read_v1' },
      evidence: { controlUrl: `http://127.0.0.1:${(server.address() as any).port}`, token: 'runtime-secret', requestId: 'd', taskId: 't', workerId: 'w', contextId: 'c', ensureActive: async () => {} } });
    const pending = worker.run();
    await reading;
    const start = Date.now();
    worker.agent.abort();
    await assert.rejects(pending);
    assert.ok(Date.now() - start < 1000);
    assert.equal(worker.observations.length, 0);
  } finally { server.closeAllConnections(); await new Promise<void>(r => server.close(() => r())); }
});
