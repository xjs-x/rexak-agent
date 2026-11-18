import { createHmac } from 'node:crypto';
import type { AgentTool } from '@earendil-works/pi-agent-core';
import { validateControlUrl, type EvidenceChannel } from './evidence.js';
import { GRAPH_READ_SCHEMA } from './graph-contract.js';

/** No Session, credentials, URL, graph revision mutation or maintenance action is model-selectable. */
export function graphTools(channel: EvidenceChannel) {
  const base = validateControlUrl(channel.controlUrl);
  const signature = createHmac('sha256', channel.token).update(`graph:v1:${channel.requestId}:${channel.taskId}:${channel.workerId}:${channel.contextId}`).digest('hex');
  async function call(action: string, args: object, signal?: AbortSignal) {
    await channel.ensureActive();
    const response = await fetch(`${base}/api/internal/delegations/${channel.requestId}/graph`, {
      method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${signature}`, 'Content-Type': 'application/json' },
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      body: JSON.stringify({ ...args, action, task_id: channel.taskId, worker_id: channel.workerId, context_id: channel.contextId }),
    });
    if (!response.ok) throw new Error(`Graph controller rejected read (${response.status})`);
    const raw = await response.text();
    if (Buffer.byteLength(raw) > 65536) throw new Error('Graph response exceeds transport budget');
    await channel.ensureActive();
    if (signal?.aborted) throw new Error('Graph read cancelled');
    return JSON.parse(raw);
  }
  const definitions = [
    ['graph_overview', 'overview', '确定性全局索引：统计、分类样本、端点、来源和未展示数量；不是完整图，不确认历史页。', ['token_budget', 'category_limit']],
    ['graph_query', 'query', '只读查询全 Session 图：ID/关键词/状态/结果过滤、分页、方向/1至4跳展开与真实跨图引用。返回 revision、total、has_more、来源；旧游标明确拒绝。', ['query']],
    ['read_shared_graph', 'read', '按序列化估算与预算读取双图，返回 full / overview / omitted；大图可用 graph_query 分页完整读取。', ['token_budget']],
  ] as const;
  const tools: AgentTool[] = definitions.map(([name, action, description, fields]) => ({
    name, label: name, description,
    parameters: { type: 'object', properties: Object.fromEntries(fields.map(f => [f, GRAPH_READ_SCHEMA.properties[f]])), additionalProperties: false, ...(name === 'graph_query' ? { required: ['query'] } : {}) },
    async execute(_id, args, signal) {
      const value = await call(action, args as object, signal);
      return { content: [{ type: 'text' as const, text: JSON.stringify(value) }], details: value, ...(value.error ? { isError: true } : {}) };
    },
  }));
  return { tools, initial: (token_budget: number, signal?: AbortSignal) => call('read', { token_budget },
    signal ? AbortSignal.any([signal, AbortSignal.timeout(1500)]) : AbortSignal.timeout(1500)) };
}
