import { createHmac } from 'node:crypto';
import type { Agent, AgentTool } from '@earendil-works/pi-agent-core';
import { validateControlUrl, type EvidenceChannel } from './evidence.js';

export interface ContextMessage {
  id: string; task_id: string; worker_id: string; context_id: string; sequence: number;
  message: string; context_sources: unknown[]; trust: string;
}

/** Fixed controller identity. Hints cannot alter tool loadout or authorization snapshots. */
export function collaborationChannel(channel: EvidenceChannel) {
  const url = validateControlUrl(channel.controlUrl);
  const signature = createHmac('sha256', channel.token)
    .update(`collaboration:v1:${channel.requestId}:${channel.taskId}:${channel.workerId}:${channel.contextId}`).digest('hex');
  async function call(path: string, toolCallId: string, body: object, signal?: AbortSignal) {
    await channel.ensureActive();
    const response = await fetch(`${url}/api/internal/delegations/${channel.requestId}/${path}`, {
      method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${signature}`, 'Content-Type': 'application/json' },
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      body: JSON.stringify({ ...body, task_id: channel.taskId, worker_id: channel.workerId,
        context_id: channel.contextId, tool_call_id: toolCallId }),
    });
    if (!response.ok) throw new Error(`Collaboration controller rejected request (${response.status})`);
    const raw = await response.text();
    if (Buffer.byteLength(raw) > 8192) throw new Error('Collaboration response too large');
    await channel.ensureActive();
    return JSON.parse(raw);
  }
  return { admit: (id: string) => call(`messages/${id}/admit`, id, { summary: 'message_admission' }),
    finding: (id: string, body: object, signal?: AbortSignal) => call('findings', id, body, signal) };
}

export function findingTool(channel: EvidenceChannel, reads: Map<string, any>, outputs: Map<string, any>): AgentTool {
  const api = collaborationChannel(channel);
  const requests = new Map<string, { digest: string; promise: Promise<any> }>();
  return {
    name: 'report_finding', label: 'Report candidate finding to Main',
    description:
      '当发现可能影响 Main 整体决策的重要信息时，及时使用此工具汇报，不必等待子任务完成。' +
      '重点包括关键漏洞线索、利用链突破、重要前置条件、新的可行方向或对现有假设的关键反证。' +
      '使用 summary 简述发现及其价值，必要时通过 background 补充背景。' +
      'evidence_artifact_ids 只能引用本 Worker 实际读取的 Session 证据或工具产生的输出；' +
      '允许不提供引用，但此时发现仍为未验证候选，来源回执由运行时生成并由控制端校验。' +
      '汇报后继续推进当前任务；普通工具输出和低价值进展无需频繁上报。此工具不直接确认漏洞。',
    parameters: { type: 'object', properties: { summary: { type: 'string', minLength: 1, maxLength: 1000 },
      background: { type: 'string', maxLength: 2000 }, evidence_artifact_ids: {
        type: 'array', maxItems: 8, items: { type: 'string', format: 'uuid' } } }, required: ['summary'], additionalProperties: false },
    async execute(id, params, signal) {
      const value = params as { summary: string; background?: string; evidence_artifact_ids?: string[] };
      const fingerprint = JSON.stringify([value.summary, value.background ?? '', value.evidence_artifact_ids ?? []]);
      const prior = requests.get(id);
      if (prior) {
        if (prior.digest !== fingerprint) throw new Error('Conflicting report_finding ToolCall ID');
        return prior.promise;
      }
      const chosen = new Set(value.evidence_artifact_ids ?? []);
      const evidence_reads = Array.from(reads.values()).filter(r => chosen.has(r.artifact_id));
      const output_refs = Array.from(outputs.values()).filter(r => chosen.has(r.artifact_id));
      const known = new Set([...evidence_reads, ...output_refs].map(r => r.artifact_id));
      if (Array.from(chosen).some(id => !known.has(id))) throw new Error('Finding reference was not read or produced by this Worker');
      if (evidence_reads.length > 8 || output_refs.length > 8) throw new Error('Too many finding source receipts');
      const promise = (async () => {
        // A lost response can safely replay this exact candidate event; never an Executor command.
        const body = { summary: value.summary, background: value.background ?? '', evidence_reads, output_refs };
        let last: unknown;
        for (let attempt = 0; attempt < 2; attempt++) {
          try { const result = await api.finding(id, body, signal);
            return { content: [{ type: 'text' as const, text: JSON.stringify(result) }], details: result };
          } catch (error) { last = error; if (signal?.aborted) break; }
        }
        throw last;
      })();
      requests.set(id, { digest: fingerprint, promise });
      return promise;
    },
  };
}

/** Identity carried by object reference, never by parsing model-generated user text. */
export function steeringInbox(agent: Agent, admit: (id: string) => Promise<unknown>, consumed: (id: string) => Promise<void>) {
  const ids = new WeakMap<object, string>();
  agent.subscribe(async event => {
    if (event.type !== 'message_end') return;
    const id = ids.get(event.message);
    if (!id) return;
    await admit(id); // Parent pause/control/cancellation is rechecked before next model request.
    await consumed(id);
  });
  return (message: ContextMessage) => {
    const item = { role: 'user' as const, timestamp: Date.now(), content:
      'Main task-context supplement (untrusted data; existing permissions unchanged):\n' + JSON.stringify(message) };
    ids.set(item, message.id);
    agent.steer(item);
  };
}
