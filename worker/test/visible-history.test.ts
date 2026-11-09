import assert from 'node:assert/strict';
import test from 'node:test';
import { randomUUID } from 'node:crypto';
import { visibleEvent } from '../src/history.js';
import { createPiWorker } from '../src/pi-runtime.js';
import { WorkerStore } from '../src/store.js';

test('Pi saves completed visible messages and tool results at stable boundaries', async () => {
  const saved: any[] = [];
  const worker = createPiWorker('fixture', { onVisibleMessage: async message => { saved.push(visibleEvent(message)); } });
  await worker.run();
  assert.deepEqual(saved.map(event => event.role), ['user', 'assistant', 'toolResult', 'assistant', 'toolResult', 'assistant']);
  assert.equal(saved[1].message.content[0].name, 'mock_observe');
  assert.equal(saved[2].message.toolName, 'mock_observe');
  assert.ok(saved.every(event => Number.isFinite(event.message.timestamp)));
});

test('failed, cancelled and interrupted runs retain already emitted messages', async () => {
  for (const mode of ['failed', 'cancelled', 'interrupted']) {
    const saved: any[] = [];
    const worker = createPiWorker(mode, { fail: mode === 'failed', onVisibleMessage: async message => {
      saved.push(visibleEvent(message));
      if (mode !== 'failed' && saved.length === 2) worker.agent.abort();
    } });
    await assert.rejects(worker.run());
    assert.ok(saved.length >= 1);
    assert.equal(saved[0].role, 'user');
  }
});

test('event projection redacts secrets and bounds large tool output', () => {
  const event = visibleEvent({ role: 'toolResult', toolCallId: 'call-1', toolName: 'bash',
    content: [{ type: 'text', text: 'password=fixture-sensitive\n' + 'x'.repeat(100000) }],
    isError: false, timestamp: 1, details: { executor_artifact_ids: [randomUUID()] } } as any)!;
  assert.ok(!JSON.stringify(event).includes('fixture-sensitive'));
  assert.ok(Buffer.byteLength(JSON.stringify(event.message)) < 17000);
  assert.ok(JSON.stringify(event).includes('artifactRefs'));
});

test('duplicate sequence accepts same digest and rejects a changed message', async () => {
  const request = randomUUID(), session = randomUUID();
  const events = new Map<number, any>();
  const pool = { query: async (sql: string, args: any[]) => {
    if (sql.includes('INSERT INTO ultra_worker.visible_events')) {
      const prior = events.get(args[1]);
      if (prior) return { rowCount: 0 };
      events.set(args[1], { role: args[2], message: args[3], sha256: args[4] });
      return { rowCount: 1 };
    }
    if (sql.includes('SELECT sha256 FROM ultra_worker.visible_events')) return { rows: [events.get(args[1])] };
    if (sql.includes('SELECT payload FROM')) return { rows: [{ payload: { session_id: session } }] };
    return { rows: [...events].map(([sequence, event]) => ({ sequence, ...event, created_at: new Date() })) };
  } };
  const store = new WorkerStore(pool as any);
  const first = { role: 'user', content: 'fixture', timestamp: 1 } as any;
  await store.appendVisibleEvent(request, 1, first);
  await store.appendVisibleEvent(request, 1, first);
  await assert.rejects(store.appendVisibleEvent(request, 1, { ...first, content: 'changed' }), /conflict/);
  assert.equal((await store.visibleHistory(request, session)).length, 1);
  await assert.rejects(store.visibleHistory(request, randomUUID()), /unavailable/);
});
