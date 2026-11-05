import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import pg from 'pg';
import { createPiWorker } from '../src/pi-runtime.js';
import { steeringInbox, type ContextMessage } from '../src/collaboration.js';
import { WorkerStore } from '../src/store.js';

function message(task_id = randomUUID(), worker_id = randomUUID(), context_id = randomUUID(), sequence = 1): ContextMessage {
  return { id: randomUUID(), task_id, worker_id, context_id, sequence, message: 'synthetic-clue',
    context_sources: [], trust: 'untrusted_task_context_no_authority_change' };
}

test('native steer waits for current tool and emits an awaited consumption receipt', async () => {
  const worker = createPiWorker('synthetic', { delayMs: 60 });
  let toolRunning = false;
  const consumed: string[] = [];
  let open!: () => void;
  const started = new Promise<void>(resolve => { open = resolve; });
  worker.agent.subscribe(event => {
    if (event.type === 'tool_execution_start') { toolRunning = true; open(); }
    if (event.type === 'tool_execution_end') toolRunning = false;
  });
  const queue = steeringInbox(worker.agent, async () => { assert.equal(toolRunning, false); }, async id => { consumed.push(id); });
  const run = worker.run();
  await started;
  const hint = message();
  queue(hint);
  assert.equal(toolRunning, true);
  assert.deepEqual(consumed, []);
  await run;
  assert.deepEqual(consumed, [hint.id]);
  assert.equal(worker.agent.state.messages.filter(m => m.role === 'user').length, 2);
});

const enabled = !!process.env.REXAK_TEST_DATABASE_URL;
const pool = new pg.Pool({ connectionString: process.env.REXAK_TEST_DATABASE_URL, max: 3 });
after(async () => { await pool.end(); });
test('durable inbox deduplicates, rejects conflicts/gaps/cancel and never invents resume', { skip: !enabled }, async () => {
  const store = new WorkerStore(pool);
  const context = randomUUID();
  const { record } = await store.reserve(randomUUID(), 'fixture', { objective: 'synthetic', context_refs: [],
    context_id: context, collaboration_access: 'collaboration_v1' });
  await store.claim(record.task_id);
  const hint = message(record.task_id, record.worker_id, context);
  assert.equal((await store.reserveMessage(record.request_id, hint, 'digest')).created, true);
  assert.equal((await store.reserveMessage(record.request_id, hint, 'digest')).created, false);
  await assert.rejects(store.reserveMessage(record.request_id, hint, 'different'), /conflict/);
  await assert.rejects(store.reserveMessage(record.request_id, { ...hint, id: randomUUID(), sequence: 3 }, 'gap'), /gap/);
  await store.messageStatus(hint.id, 'consumed');
  await store.messageStatus(hint.id, 'queued');
  assert.equal((await store.message(record.request_id, hint.id)).status, 'consumed');
  const pending = message(record.task_id, record.worker_id, context, 2);
  await store.reserveMessage(record.request_id, pending, 'pending');
  await store.messageStatus(pending.id, 'queued');
  await store.cancelRequest(record.request_id);
  await assert.rejects(store.reserveMessage(record.request_id, { ...pending, id: randomUUID(), sequence: 3 }, 'late'), /unavailable/);
  await store.recoverInterrupted();
  assert.equal((await store.message(record.request_id, hint.id)).status, 'consumed');
  assert.equal((await store.message(record.request_id, pending.id)).status, 'context_lost');
});
