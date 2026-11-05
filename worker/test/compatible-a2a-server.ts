/** Offline transport fixture: real Pi provider serializer, SDK and PostgreSQL, no target/provider traffic. */
import { createWorkerServer } from '../src/server.js';
import type { WorkerModelConfig } from '../src/model-provider.js';
const config: WorkerModelConfig = { kind: 'openai-compatible', provider: 'bailian', model: 'fixture-deepseek',
  base_url: 'https://fixture.example/compatible-mode/v1', api_key: 'isolated-fixture-key', temperature: 1,
  max_tokens: 1000, context_window: 16000, timeout_ms: Number(process.env.FIXTURE_MODEL_TIMEOUT ?? 5000) };
globalThis.fetch = async (_input, init) => {
  if (process.env.FIXTURE_PROVIDER_HANG === 'true') return new Promise<Response>((_resolve, reject) => {
    const abort = () => reject(new DOMException('Aborted', 'AbortError'));
    if (init?.signal?.aborted) abort(); else init?.signal?.addEventListener('abort', abort, { once: true });
  });
  const body = JSON.parse(String(init?.body));
  const turn = body.messages.filter((m: { role: string }) => m.role === 'tool').length;
  const delta = turn < 2 ? { role: 'assistant', tool_calls: [{ index: 0, id: `fixture_${turn}`, type: 'function', function: { name: 'mock_observe', arguments: JSON.stringify({ text: `isolated observation ${turn}` }) } }] }
    : { role: 'assistant', content: 'Two synthetic observations; no target verification.' };
  const chunks = [ { choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: turn < 2 ? 'tool_calls' : 'stop' }] },
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } } ];
  return new Response(chunks.map(c => 'data: ' + JSON.stringify({ id: 'fixture', model: config.model, ...c }) + '\n\n').join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
};
const port = Number(process.env.FIXTURE_PORT);
const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_WORKER_DATABASE_URL!, token: process.env.REXAK_WORKER_TOKEN!, baseUrl: `http://127.0.0.1:${port}`, modelConfig: config });
runtime.app.listen(port, '127.0.0.1');
