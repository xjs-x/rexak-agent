import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import pg from 'pg';
import { createPiWorker } from '../src/pi-runtime.js';
import { snapshot, restore, HISTORY_LIMIT } from '../src/history.js';
import { WorkerStore } from '../src/store.js';
import { Artifact, TaskState } from '@a2a-js/sdk';

test('native Pi history retains tool pairing, drops private reasoning and uses fresh bindings', async () => {
  const first = createPiWorker('original'); await first.run();
  const messages: any[] = structuredClone(first.agent.state.messages);
  messages.find(m => m.role === 'assistant').content.push({ type: 'thinking', thinking: 'PRIVATE', thinkingSignature: 'PRIVATE' });
  const saved = snapshot(messages);
  assert.ok(!JSON.stringify(saved).includes('PRIVATE'));
  assert.ok(!saved.messages.some((m: any) => m.role === 'system'));
  const next = createPiWorker('continue', { history: restore(saved, saved.sha256) });
  await next.run();
  assert.equal(next.observations.length, 0); // Actual earlier tool results inform the native next turn.
  assert.equal(next.usage().calls, 1); // Prior model turns are not charged to the new delegation.
  assert.ok(next.agent.state.messages.length > first.agent.state.messages.length);
  assert.notEqual(next.agent.state.tools[0].execute, first.agent.state.tools[0].execute);
  assert.throws(() => restore(saved, '0'.repeat(64)), /checksum/);
  const damaged = structuredClone(messages).filter(m => m.role !== 'toolResult');
  assert.throws(() => snapshot(damaged), /pending tools/);
  assert.throws(() => snapshot([{ role: 'user', content: 'x'.repeat(HISTORY_LIMIT), timestamp: 1 }] as any), /size limit/);
  const credentials = structuredClone(messages);
  credentials.find(m => m.role === 'toolResult').content = [{ type: 'text', text: 'password=fixture-sensitive Bearer fixture-sensitive' }];
  assert.ok(!JSON.stringify(snapshot(credentials)).includes('fixture-sensitive'));
  const overContext = createPiWorker('continue', { history: [
    { role: 'user', content: 'x'.repeat(9000), timestamp: 1 }, ...saved.messages.slice(1),
  ] as any });
  await assert.rejects(overContext.run(), /context capacity/);
});

test('completed history survives store restart; cancelled and cross-session sources fail', { skip: !process.env.REXAK_TEST_DATABASE_URL }, async () => {
  const pool = new pg.Pool({ connectionString: process.env.REXAK_TEST_DATABASE_URL });
  try {
    const store = new WorkerStore(pool);
    const session = randomUUID(), id = randomUUID();
    const payload = { objective: 'history', context_refs: [], session_id: session, context_id: randomUUID() };
    const r = await store.reserve(id, 'history-test', payload);
    assert.ok(await store.claim(r.record.task_id));
    const worker = createPiWorker('history'); await worker.run();
    await store.complete(r.record.task_id, TaskState.TASK_STATE_COMPLETED, Artifact.fromJSON({ artifactId: randomUUID(), parts: [] }), worker.agent.state.messages);
    const restarted = new WorkerStore(pool); await restarted.recoverInterrupted();
    assert.ok((await restarted.history(id, session)).some(m => m.role === 'toolResult'));
    await assert.rejects(restarted.history(id, randomUUID()), /unavailable/);
    await store.cancelRequest(id);
    await assert.rejects(restarted.history(id, session), /unavailable/);
  } finally { await pool.end(); }
});
