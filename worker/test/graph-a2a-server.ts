/** Synthetic provider for the real Main -> A2A -> native Pi -> controller graph path. */
import { createWorkerServer } from '../src/server.js';
import type { WorkerModelConfig } from '../src/model-provider.js';
const originalFetch = globalThis.fetch;
let checkpoint = false;
let release: () => void = () => {};
const barrier = new Promise<void>(resolve => { release = resolve; });
const requests: { bytes: number; initialMode?: string; toolResults: number }[] = [];
const graphReads: { durationMs: number; status: number }[] = [];
globalThis.fetch = async (url, init) => {
  if (!String(url).startsWith('https://fixture.example/')) {
    const start = performance.now();
    const result = await originalFetch(url, init);
    if (String(url).endsWith('/graph')) graphReads.push({ durationMs: Math.round((performance.now() - start) * 1000) / 1000, status: result.status });
    return result;
  }
  const body = JSON.parse(String(init?.body));
  const results = body.messages.filter((m: any) => m.role === 'tool').map((m: any) => JSON.parse(m.content));
  const initial = body.messages.find((m: any) => m.role === 'user' && String(m.content).startsWith('当前 Session 共享双图'));
  requests.push({ bytes: Buffer.byteLength(JSON.stringify(body)), initialMode: initial ? JSON.parse(initial.content.split('\n')[1]).mode : undefined, toolResults: results.length });
  if (results.some((r: any) => r.error)) throw new Error('Synthetic graph tool failed');
  let name = 'graph_query', args: Record<string, unknown> = { query: { keyword: 'unlisted entity' } }, final: string | undefined;
  if (results.length === 1) args = { query: { ids: [results[0].records[0].data.id], cross_graph: true } };
  else if (results.length === 2) args = { query: { graph: 'attack', ids: [results[1].records.find((r: any) => r.graph === 'attack').data.id], expand: true } };
  else if (results.length === 3) {
    name = 'read_evidence';
    args = { artifact_id: results[2].records.find((r: any) => r.kind === 'edges').data.source_refs[0].split(':')[1] };
  } else if (results.length === 4) {
    checkpoint = true;
    await barrier;
    name = 'graph_overview'; args = {};
  } else if (results.length === 5) {
    final = JSON.stringify({ oldRevision: results[0].revision, newRevision: results[4].revision,
      hiddenEntity: results[0].records[0].data.label, evidence: results[3].data.text,
      graphToolCalls: 4, evidenceToolCalls: 1, extraModelCalls: results.length, requests, graphReads });
  }
  const delta = final ? { role: 'assistant', content: final }
    : { role: 'assistant', tool_calls: [{ index: 0, id: `graph_${results.length}`, type: 'function', function: { name, arguments: JSON.stringify(args) } }] };
  const events = [{ choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: final ? 'stop' : 'tool_calls' }] },
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } }];
  return new Response(events.map(c => `data: ${JSON.stringify({ id: 'graph-model', model: 'fixture', ...c })}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
};
const config: WorkerModelConfig = { kind: 'openai-compatible', provider: 'bailian', model: 'fixture-graph-model',
  base_url: 'https://fixture.example/compatible-mode/v1', api_key: 'fixture-only-key', temperature: 1,
  max_tokens: 2000, context_window: 32000, timeout_ms: 10000 };
const port = Number(process.env.FIXTURE_PORT);
const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_WORKER_DATABASE_URL!, token: process.env.REXAK_WORKER_TOKEN!,
  baseUrl: `http://127.0.0.1:${port}`, modelConfig: config, controlUrl: process.env.REXAK_WORKER_CONTROL_URL });
runtime.app.get('/graph-checkpoint', (_req, res) => res.json({ checkpoint }));
runtime.app.post('/graph-release', (_req, res) => { release(); res.json({ ok: true }); });
runtime.app.listen(port, '127.0.0.1');
