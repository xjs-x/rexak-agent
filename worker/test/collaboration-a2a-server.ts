/** Physical provider transport is deterministic; all agent loops, tools and A2A are real. */
import { createWorkerServer } from '../src/server.js';
import type { WorkerModelConfig } from '../src/model-provider.js';
const original = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  if (!String(url).startsWith('https://fixture.example/')) return original(url, init);
  const body = JSON.parse(String(init?.body));
  const results = body.messages.filter((m: any) => m.role === 'tool');
  const users = body.messages.filter((m: any) => m.role === 'user').map((m: any) => ({
    ...m, content: typeof m.content === 'string' ? m.content : m.content.map((p: any) => p.text ?? '').join('\n') }));
  const task = JSON.parse(users[0].content.split('\n')[1]);
  const currentTask = JSON.parse(users.filter((m: any) => m.content.startsWith('Delegated task package')).at(-1).content.split('\n')[1]);
  if (currentTask.resume_from_delegation_id) {
    const previous = results.some((m: any) => (m.content.includes('worker-ok') || m.content.includes('synthetic-goal-result:recovered-clue')));
    const clue = users.some((m: any) => m.content.includes('stage4-main-clue'));
    if (!previous || !clue) throw new Error('Real historical tool output and steering missing');
    const text = `synthetic-goal-result: recovered-clue; historical-tool-output=${previous}; prior-supplement=${clue}`;
    const events = [{ choices: [{ index: 0, delta: { role: 'assistant', content: text }, finish_reason: null }] },
      { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }];
    return new Response(events.map(c => `data: ${JSON.stringify({ id: 'resumed', model: 'fixture', ...c })}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
  }
  const sourceOutput = results[0];
  const outputId = sourceOutput?.content.match(/artifact=([0-9a-f-]+) stdout/)?.[1];
  const supplement = users.filter((m: any) => m.content.includes('Main task-context supplement'));
  const turn = results.length;
  const sequence: [string, object][] = [
    ['bash', { command: task.objective.startsWith('Explore synthetic route') ? 'printf synthetic-goal-result:recovered-clue' : 'printf stage4-initial' }],
    ['read_evidence', { artifact_id: task.context_refs[0] }],
    ['report_finding', { summary: 'stage4-important-hint', background: 'Candidate, not independently verified.',
      evidence_artifact_ids: [task.context_refs[0], ...(outputId ? [outputId] : [])] }],
    ['bash', { command: 'sleep 2; printf stage4-wait' }],
  ];
  if (task.objective === 'Explore synthetic route 1') sequence.push(['bash', { command: 'sleep 2; printf stage4-wait' }], ['bash', { command: 'sleep 2; printf stage4-wait' }]);
  const item = sequence[turn];
  const delta = item ? { role: 'assistant', tool_calls: [{ index: 0, id: `collab_${turn}`, type: 'function',
    function: { name: item[0], arguments: JSON.stringify(item[1]) } }] }
    : { role: 'assistant', content: `Worker used ${supplement.length} supplements; clue=${supplement.some((m: any) => m.content.includes('stage4-main-clue')) ? 'stage4-main-clue' : 'missing'}; source-output-present=${!!outputId}` };
  const events = [{ choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: item ? 'tool_calls' : 'stop' }] },
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } }];
  return new Response(events.map(c => `data: ${JSON.stringify({ id: 'collab-model', model: 'fixture', ...c })}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
};
const config: WorkerModelConfig = { kind: 'openai-compatible', provider: 'bailian', model: 'fixture-collaboration',
  base_url: 'https://fixture.example/compatible-mode/v1', api_key: 'fixture-only-key', temperature: 1,
  max_tokens: 2000, context_window: 32000, timeout_ms: 10000 };
const port = Number(process.env.FIXTURE_PORT);
const runtime = await createWorkerServer({ databaseUrl: process.env.REXAK_WORKER_DATABASE_URL!, token: process.env.REXAK_WORKER_TOKEN!,
  baseUrl: `http://127.0.0.1:${port}`, modelConfig: config, controlUrl: process.env.REXAK_WORKER_CONTROL_URL });
runtime.app.listen(port, '127.0.0.1');
