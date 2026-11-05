import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createPiWorker } from '../src/pi-runtime.js';
import { card } from '../src/server.js';

test('Pi owns a multi-turn model/tool loop and emits execution events', async () => {
  const worker = createPiWorker('inspect synthetic alpha');
  const summary = await worker.run();
  assert.match(summary, /2 synthetic observations/);
  assert.ok(!summary.includes("inspect synthetic alpha"));
  assert.equal(worker.observations.length, 2);
  assert.equal(worker.events.filter(e => e === 'tool_execution_end').length, 2);
  assert.equal(worker.events.filter(e => e === 'turn_end').length, 3);
});

test('parallel Workers have independent contexts and identities', async () => {
  const alpha = createPiWorker('private-alpha');
  const beta = createPiWorker('private-beta');
  await Promise.all([alpha.run(), beta.run()]);
  assert.ok(JSON.stringify(alpha.agent.state.messages).includes('private-alpha'));
  assert.ok(!JSON.stringify(alpha.agent.state.messages).includes('private-beta'));
  assert.ok(!JSON.stringify(beta.agent.state.messages).includes('private-alpha'));
  assert.notEqual(alpha.agent, beta.agent);
});

test('aborting Pi stops the mock tool loop without claiming Executor termination', async () => {
  const worker = createPiWorker('cancel me', { delayMs: 1000 });
  const pending = worker.run();
  setTimeout(() => worker.agent.abort(), 20);
  await assert.rejects(pending);
  assert.equal(worker.observations.length, 0);
});

test('provider error is surfaced as failed work', async () => {
  await assert.rejects(createPiWorker('fail', { fail: true }).run());
});

test('Agent Card declares only A2A 1.0 polling and mock capabilities', () => {
  const value = card('http://127.0.0.1:8795');
  assert.equal(value.capabilities?.streaming, false);
  assert.equal(value.capabilities?.pushNotifications, false);
  assert.equal(value.supportedInterfaces[0].protocolVersion, '1.0');
  assert.equal(value.securitySchemes.bearer.scheme?.$case, 'httpAuthSecurityScheme');
});
