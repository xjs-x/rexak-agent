import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stream as nativeStream } from '@earendil-works/pi-ai/api/openai-completions';
import { createPiWorker } from '../src/pi-runtime.js';
import { validateModelConfig, type WorkerModelConfig } from '../src/model-provider.js';
import { validTaskPackage } from '../src/task-package.js';

const config: WorkerModelConfig = { kind: 'openai-compatible', provider: 'bailian', model: 'deepseek-v4.1-flash',
  base_url: 'https://fixture.example/compatible-mode/v1', api_key: 'fixture-secret-never-echo', temperature: 1,
  max_tokens: 393216, context_window: 1000000, timeout_ms: 5000 };
function streamBody(turn: number, usage = true) {
  const delta = turn < 2 ? { role: 'assistant', reasoning_content: 'synthetic-reasoning',
    tool_calls: [{ index: 0, id: `call_${turn}`, type: 'function', function: { name: 'mock_observe', arguments: JSON.stringify({ text: `synthetic-${turn}` }) } }] }
    : { role: 'assistant', content: 'Synthetic observations collected; no target verification.' };
  const chunks = [
    { choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: turn < 2 ? 'tool_calls' : 'stop' }] },
    ...(usage ? [{ choices: [], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120, prompt_tokens_details: { cached_tokens: 30 } } }] : []),
  ];
  return chunks.map(c => 'data: ' + JSON.stringify({ id: `completion-${turn}`, object: 'chat.completion.chunk', created: 1, model: config.model, ...c }) + '\n\n').join('') + 'data: [DONE]\n\n';
}
function fakeTransport(body: (turn: number) => Response | Promise<Response>) {
  const payloads: Record<string, any>[] = [];
  const transport: typeof nativeStream = (model, context, options) => nativeStream(model, context, {
    ...options, fetch: async (_url, init) => {
      payloads.push(JSON.parse(String(init?.body)));
      return body(payloads.length - 1);
    },
  });
  return { transport, payloads };
}
test('native OpenAI-compatible provider performs three turns with mock tools and reported usage', async () => {
  const fake = fakeTransport(turn => new Response(streamBody(turn), { headers: { 'content-type': 'text/event-stream' } }));
  const task = { objective: 'open exploration', context_refs: [], background: 'background-marker',
    completion_requirements: ['record two independent synthetic observations'], context_id: 'd536858e-861b-402a-81ee-58ebda1f33d1', handoff_version: 1 as const, context_sources: [] };
  let starts = 0, finishes = 0;
  const worker = createPiWorker(task.objective, { task, modelConfig: config, providerTransport: fake.transport,
    callHooks: { start: async () => { starts++; }, finish: async () => { finishes++; } } });
  await worker.run();
  assert.equal(worker.observations.length, 2);
  assert.equal(starts, 3); assert.equal(finishes, 3); assert.equal(fake.payloads.length, 3);
  assert.ok(JSON.stringify(fake.payloads[0].messages).includes('background-marker'));
  assert.ok(JSON.stringify(fake.payloads[0].messages).includes('completion_requirements'));
  assert.equal(fake.payloads[1].messages.filter((m: any) => m.role === 'tool').length, 1);
  assert.ok(fake.payloads[1].messages.some((m: any) => m.reasoning_content === 'synthetic-reasoning'));
  assert.equal(fake.payloads[0].temperature, 1); assert.equal(fake.payloads[0].max_tokens, 393216);
  assert.deepEqual(fake.payloads[0].stream_options, { include_usage: true });
  assert.equal(fake.payloads[0].messages[0].role, 'system');
  assert.ok(!JSON.stringify(fake.payloads).includes(config.api_key));
  assert.equal(worker.usage().input_tokens, 210); assert.equal(worker.usage().cache_read_tokens, 90);
  assert.equal(worker.usage().output_tokens, 60); assert.equal(worker.usage().usage_status, 'reported');
});
test('missing provider usage stays unknown, never free', async () => {
  const fake = fakeTransport(() => new Response(streamBody(2, false), { headers: { 'content-type': 'text/event-stream' } }));
  const worker = createPiWorker('fixture', { modelConfig: config, providerTransport: fake.transport });
  await worker.run();
  assert.equal(worker.usage().usage_status, 'unknown'); assert.equal(worker.usage().input_tokens, null);
});
for (const status of [401, 429]) test(`provider ${status} is sanitized and has no hidden retries`, async () => {
  const reason = status === 401 ? 'provider_authentication_error' : 'provider_rate_limited';
  const fake = fakeTransport(() => new Response(JSON.stringify({ error: { message: status + ' provider rejected request ' + config.api_key } }), { status, headers: { 'content-type': 'application/json' } }));
  const worker = createPiWorker('fixture', { modelConfig: config, providerTransport: fake.transport });
  await assert.rejects(worker.run(), new RegExp(reason));
  assert.equal(fake.payloads.length, 1); assert.equal(worker.metrics[0].usage_status, 'unknown');
  assert.ok(!JSON.stringify(worker.metrics).includes(config.api_key));
  assert.ok(!worker.agent.state.errorMessage?.includes(config.api_key));
});
for (const mode of ['timeout', 'cancel'] as const) test(`${mode} aborts the native in-flight provider`, async () => {
  let calls = 0;
  const worker = createPiWorker('fixture', { modelConfig: { ...config, timeout_ms: mode === 'timeout' ? 30 : 5000 },
    providerTransport: (model, context, options) => nativeStream(model, context, { ...options,
      fetch: async (_url, init) => { calls++; return new Promise<Response>((_resolve, reject) => {
        const abort = () => reject(new DOMException('Aborted', 'AbortError'));
        if (init?.signal?.aborted) abort(); else init?.signal?.addEventListener('abort', abort, { once: true });
      }); },
    }) });
  const pending = worker.run();
  if (mode === 'cancel') setTimeout(() => worker.agent.abort(), 30);
  await assert.rejects(pending);
  assert.equal(calls, 1); assert.equal(worker.observations.length, 0);
  assert.equal(worker.metrics[0].failure_reason, mode === 'timeout' ? 'provider_timeout' : 'provider_cancelled');
  assert.equal(worker.metrics[0].input_tokens, null);
});
test('measurement intent failure prevents provider transport', async () => {
  const fake = fakeTransport(() => { throw new Error('must not call'); });
  const worker = createPiWorker('fixture', { modelConfig: config, providerTransport: fake.transport,
    callHooks: { start: async () => { throw new Error('database unavailable'); } } });
  await assert.rejects(worker.run()); assert.equal(fake.payloads.length, 0);
});
test('measurement settlement failure stops Pi and preserves reported usage', async () => {
  const fake = fakeTransport(() => new Response(streamBody(2), { headers: { 'content-type': 'text/event-stream' } }));
  const worker = createPiWorker('fixture', { modelConfig: config, providerTransport: fake.transport,
    callHooks: { finish: async () => { throw new Error('database unavailable'); } } });
  await assert.rejects(worker.run(), /telemetry_persistence_failed/);
  assert.equal(fake.payloads.length, 1);
  assert.equal(worker.metrics[0].usage_status, 'reported');
  assert.equal(worker.metrics[0].output_tokens, 20);
});
test('invalid model config and forged handoff fields are rejected', () => {
  assert.throws(() => validateModelConfig({ ...config, api_key: '' }));
  assert.throws(() => validateModelConfig({ ...config, base_url: 'http://127.0.0.1' }));
  assert.throws(() => validateModelConfig({ ...config, provider: 'other' }));
  assert.ok(validTaskPackage({ objective: 'open exploration', context_refs: [] }));
  assert.ok(!validTaskPackage({ objective: 'x', context_refs: [], api_key: 'forged' }));
  assert.ok(!validTaskPackage({ objective: 'x', context_refs: [], handoff_version: 1, context_id: 'forged', context_sources: [] }));
});
test('truncated model output records incomplete work instead of completion', async () => {
  const fake = fakeTransport(() => new Response(streamBody(2).replace('"finish_reason":"stop"', '"finish_reason":"length"'), { headers: { 'content-type': 'text/event-stream' } }));
  const worker = createPiWorker('fixture', { modelConfig: config, providerTransport: fake.transport });
  await assert.rejects(worker.run(), /incomplete/);
  assert.equal(worker.metrics[0].status, 'failed');
  assert.equal(worker.metrics[0].failure_reason, 'provider_output_incomplete');
});
