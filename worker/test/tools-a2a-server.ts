/** Deterministic OpenAI transport; Pi still owns every model/tool turn. */
import { createWorkerServer } from '../src/server.js';
import type { WorkerModelConfig } from '../src/model-provider.js';
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  if (!String(url).startsWith('https://fixture.example/')) return originalFetch(url, init);
  const body = JSON.parse(String(init?.body));
  const results = body.messages.filter((m: any) => m.role === 'tool');
  const sequence: [string, object][] = [
    ['write', { path: 'note.txt', content: 'Alpha' }],
    ['read', { path: 'note.txt' }],
    ['edit', { path: 'note.txt', edits: [{ oldText: 'Alpha', newText: 'Beta' }] }],
    ['read', { path: 'note.txt' }],
    ['grep', { pattern: 'Beta', path: '.' }],
    ['find', { pattern: '*.txt', path: '.' }],
    ['bash', { command: 'printf worker-ok' }],
    ['list_evidence', { query: 'stage3-history' }],
  ];
  if (process.env.FIXTURE_BASH_ONLY === 'true') sequence.splice(0, sequence.length, ['bash', { command: 'printf worker-ok' }]);
  if (results.length === 8) {
    const catalog = JSON.parse(results[7].content);
    sequence.push(['read_evidence', { artifact_id: catalog.artifacts.find((a: any) => a.summary === 'stage3-history').artifact_id }]);
  }
  const item = sequence[results.length];
  const final = !item;
  // A synthetic summary should summarize the observed text, rather than spend its
  // entire budget copying newly attached provenance identifiers.
  const summary = 'File workflow ' + results.slice(0, 7).map((r: any) => r.content.split('[Executor output sources]')[0]).join(' | ').slice(0, 800) + '; model analysis only.';
  const delta = final ? { role: 'assistant', content: summary }
    : { role: 'assistant', tool_calls: [{ index: 0, id: `tools_${results.length}`, type: 'function', function: { name: item[0], arguments: JSON.stringify(item[1]) } }] };
  const events = [{ choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: final ? 'stop' : 'tool_calls' }] },
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } }];
  return new Response(events.map(c => `data: ${JSON.stringify({ id: 'tools-model', model: 'fixture', ...c })}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
};
const config: WorkerModelConfig = { kind: 'openai-compatible', provider: 'bailian', model: 'fixture-tools-model',
  base_url: 'https://fixture.example/compatible-mode/v1', api_key: 'fixture-only-key', temperature: 1,
  max_tokens: 2000, context_window: 16000, timeout_ms: 10000 };
const port = Number(process.env.FIXTURE_PORT);
const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_WORKER_DATABASE_URL!, token: process.env.REXAK_WORKER_TOKEN!,
  baseUrl: `http://127.0.0.1:${port}`, modelConfig: config, controlUrl: process.env.REXAK_WORKER_CONTROL_URL });
runtime.app.listen(port, '127.0.0.1');
