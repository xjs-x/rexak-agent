import { createHmac } from 'node:crypto';
import type { AgentTool } from '@earendil-works/pi-agent-core';
import type { TaskPackage } from './task-package.js';

export interface EvidenceReadRef {
  receipt_id: string; artifact_id: string; sha256: string; view_sha256: string;
  offset: number; returned_bytes: number; truncated: boolean;
}
export interface EvidenceChannel {
  controlUrl: string; token: string; requestId: string; taskId: string; workerId: string; contextId: string;
  ensureActive: () => Promise<void>;
}
export function validateControlUrl(input: string): string {
  const url = new URL(input);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Invalid fixed evidence controller URL');
  return input.replace(/\/$/, '');
}
/** The model cannot select a URL, authentication identity or filesystem path. */
export function evidenceTools(channel: EvidenceChannel, task: TaskPackage) {
  const baseUrl = validateControlUrl(channel.controlUrl);
  const signature = createHmac('sha256', channel.token).update(`evidence:v1:${channel.requestId}:${channel.taskId}:${channel.workerId}:${channel.contextId}`).digest('hex');
  const reads = new Map<string, EvidenceReadRef>();
  const known = new Map((task.context_sources ?? []).map(s => [s.artifact_id, s.sha256]));
  async function call(toolCallId: string, args: Record<string, unknown>, signal?: AbortSignal) {
    await channel.ensureActive();
    if (signal?.aborted) throw new Error('Evidence read cancelled');
    const response = await fetch(`${baseUrl}/api/internal/delegations/${channel.requestId}/evidence`, {
      method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${signature}`, 'Content-Type': 'application/json' },
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      body: JSON.stringify({ ...args, task_id: channel.taskId, worker_id: channel.workerId,
        context_id: channel.contextId, tool_call_id: toolCallId }),
    });
    if (!response.ok) throw new Error(`Evidence controller rejected read (${response.status})`);
    const raw = await response.text();
    if (Buffer.byteLength(raw) > 131072) throw new Error('Evidence response too large');
    const value = JSON.parse(raw);
    await channel.ensureActive();
    if (signal?.aborted) throw new Error('Evidence read cancelled');
    if (value.ok && value.artifacts) for (const item of value.artifacts) known.set(item.artifact_id, item.sha256);
    if (value.ok && value.read_ref) {
      reads.delete(value.read_ref.artifact_id);
      reads.set(value.read_ref.artifact_id, value.read_ref);
      if (reads.size > 16) reads.delete(reads.keys().next().value!);
    }
    return { content: [{ type: 'text' as const, text: JSON.stringify(value) }], details: value,
      ...(value.ok ? {} : { isError: true }) };
  }
  const tools: AgentTool[] = [{
    name: 'list_evidence', label: 'List session evidence',
    description: 'List paginated metadata for permitted evidence in the current task Session, including evidence beyond recommended context_refs. Metadata alone does not mean content was read. Use next_cursor for more.',
    parameters: { type: 'object', properties: { query: { type: 'string', maxLength: 128 }, cursor: { type: 'string', format: 'uuid' } }, additionalProperties: false },
    execute: (id, params, signal) => call(id, { ...(params as object), action: 'list' }, signal),
  }, {
    name: 'read_evidence', label: 'Read session evidence',
    description: 'Read a verified, redacted UTF-8 evidence view by Artifact UUID. Offsets are bytes in the redacted view, NOT raw bytes. Follow next_offset when truncated. Up to 16 KiB per read. Returns source, actual range and a durable read receipt; no target access.',
    parameters: { type: 'object', properties: { artifact_id: { type: 'string', format: 'uuid' },
      offset: { type: 'integer', minimum: 0 }, limit: { type: 'integer', minimum: 4, maximum: 16384 } }, required: ['artifact_id'], additionalProperties: false },
    execute: (id, params, signal) => call(id, { ...(params as object), action: 'read',
      expected_sha256: known.get(String((params as { artifact_id: string }).artifact_id)) ?? null }, signal),
  }];
  return { tools, reads };
}
