/** Synthetic native provider makes decisions from evidence tool results, never from fixtures' secrets. */
import { createWorkerServer } from '../src/server.js';
import type { WorkerModelConfig } from '../src/model-provider.js';
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  if (!String(url).startsWith('https://fixture.example/')) return originalFetch(url, init);
  const body = JSON.parse(String(init?.body));
  const results = body.messages.filter((m: { role: string }) => m.role === 'tool').map((m: { content: string }) => JSON.parse(m.content));
  let name = 'list_evidence', args: Record<string, unknown> = {}, final: string | undefined;
  const last = results.at(-1);
  if (last?.artifacts) {
    const first = last.artifacts.find((a: { summary: string }) => a.summary === 'chain-start');
    name = 'read_evidence'; args = { artifact_id: first.artifact_id, limit: 64 };
  } else if (last?.data) {
    const data = last.data;
    name = 'read_evidence';
    if (data.truncated) args = { artifact_id: data.artifact_id, offset: data.next_offset, limit: 64 };
    else {
      const text = results.filter((r: any) => r.data?.artifact_id === data.artifact_id).map((r: any) => r.data.text).join('');
      const next = /NEXT=([0-9a-f-]{36})/.exec(text);
      if (next) args = { artifact_id: next[1], limit: 64 };
      else {
        const all = results.filter((r: any) => r.data).map((r: any) => r.data.text).join('');
        final = 'Historical evidence yields code ' + [...all.matchAll(/PART=(\d+)/g)].map(m => m[1]).join('') + '; analysis only.';
      }
    }
  } else if (last) final = 'Evidence unavailable; no confirmation.';
  const delta = final ? { role: 'assistant', content: final }
    : { role: 'assistant', tool_calls: [{ index: 0, id: `evidence_${results.length}`, type: 'function', function: { name, arguments: JSON.stringify(args) } }] };
  const events = [{ choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: final ? 'stop' : 'tool_calls' }] },
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } }];
  return new Response(events.map(c => `data: ${JSON.stringify({ id: 'evidence-model', model: 'fixture', ...c })}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
};
const config: WorkerModelConfig = { kind: 'openai-compatible', provider: 'bailian', model: 'fixture-evidence-model',
  base_url: 'https://fixture.example/compatible-mode/v1', api_key: 'fixture-only-key', temperature: 1,
  max_tokens: 2000, context_window: 16000, timeout_ms: 10000 };
const port = Number(process.env.FIXTURE_PORT);
const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_WORKER_DATABASE_URL!, token: process.env.REXAK_WORKER_TOKEN!,
  baseUrl: `http://127.0.0.1:${port}`, modelConfig: config, controlUrl: process.env.REXAK_WORKER_CONTROL_URL });
runtime.app.listen(port, '127.0.0.1');
