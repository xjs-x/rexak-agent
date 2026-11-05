import { createHash } from 'node:crypto';
import type { AgentMessage } from '@earendil-works/pi-agent-core';

export const HISTORY_LIMIT = 96 * 1024;
function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).filter(k => value[k] !== undefined).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
const digest = (messages: unknown) => createHash('sha256').update(canonical(messages)).digest('hex');
export class HistoryUnavailable extends Error {}
function checkJsonb(value: any): void {
  if (typeof value === 'string') {
    for (const char of value) {
      const point = char.codePointAt(0)!;
      if (point === 0 || (point >= 0xd800 && point <= 0xdfff)) throw new HistoryUnavailable('Pi history not representable in JSONB');
    }
  } else if (value && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) { checkJsonb(key); checkJsonb(item); }
  }
}
function redact(value: any): any {
  if (typeof value === 'string') return value
    // A header's entire value (including folded continuation) is sensitive. In inline
    // shell/JSON text the boundary is ambiguous, so conservatively remove the rest of that line.
    .replace(/(?<![a-z0-9_-])(?:proxy-authorization|authorization|set-cookie|cookie)\s*["']?\s*[:=][^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*/gi, '[HTTP credential header redacted]')
    .replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]+/gi, '$1[REDACTED]')
    .replace(/(?<![a-z0-9_-])((?:api[_-]?key|password|authorization|cookie|secret|access_token|refresh_token)\s*["']?\s*[=:]\s*["']?)[^\s"',;]+/gi, '$1[REDACTED]');
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [key, /^(api[_-]?key|password|authorization|proxy-authorization|cookie|set-cookie|secret|access_token|refresh_token)$/i.test(key) ? '[REDACTED]' : redact(item)]));
  return value;
}

/** Message-only projection: no tools, runtime state, opaque provider signatures or private thinking. */
export function snapshot(messages: AgentMessage[]) {
  checkJsonb(messages);
  const projected = messages.filter(m => m.role !== 'system').map((m: any) => {
    if (m.role === 'user') return { role: m.role, content: m.content, timestamp: m.timestamp };
    if (m.role === 'toolResult') return { role: m.role, toolCallId: m.toolCallId, toolName: m.toolName,
      content: m.content, isError: m.isError, timestamp: m.timestamp };
    if (m.role !== 'assistant') throw new HistoryUnavailable('Unsupported Pi history role');
    return { role: m.role, api: m.api, provider: m.provider, model: m.model,
      content: m.content.filter((p: any) => p.type === 'text' || p.type === 'toolCall').map((p: any) =>
        p.type === 'text' ? { type: 'text', text: p.text } : { type: 'toolCall', id: p.id, name: p.name, arguments: p.arguments }),
      stopReason: m.stopReason, usage: m.usage, timestamp: m.timestamp };
  });
  // Credential-shaped strings are redacted; tool execution bindings never enter this projection.
  const safe = JSON.parse(JSON.stringify(redact(projected)));
  validate(safe);
  return { version: 1, messages: safe, sha256: digest(safe) };
}

function validate(messages: any[]): asserts messages is AgentMessage[] {
  if (!Array.isArray(messages) || !messages.length || messages.length > 256
      || Buffer.byteLength(JSON.stringify(messages)) > HISTORY_LIMIT) throw new HistoryUnavailable('Pi history unavailable: size limit');
  const calls = new Map<string, string>();
  const seen = new Set<string>();
  for (const m of messages) {
    if (!m || !Number.isFinite(m.timestamp)) throw new HistoryUnavailable('Pi history damaged');
    if (m.role === 'toolResult') {
      if (calls.get(m.toolCallId) !== m.toolName || !Array.isArray(m.content)) throw new HistoryUnavailable('Pi history tool pairing damaged');
      calls.delete(m.toolCallId);
    } else {
      if (calls.size) throw new HistoryUnavailable('Pi history has pending tools');
      if (m.role === 'assistant') {
        if (!['stop', 'toolUse'].includes(m.stopReason) || !Array.isArray(m.content)) throw new HistoryUnavailable('Pi history incomplete');
        for (const p of m.content) {
          if (p.type === 'toolCall') {
            if (!p.id || seen.has(p.id) || typeof p.name !== 'string' || !p.arguments || typeof p.arguments !== 'object') throw new HistoryUnavailable('Pi history tool identity damaged');
            calls.set(p.id, p.name); seen.add(p.id);
          } else if (p.type !== 'text' || typeof p.text !== 'string') throw new HistoryUnavailable('Pi history contains unsupported content');
        }
      } else if (m.role !== 'user' || !(typeof m.content === 'string' || (Array.isArray(m.content) && m.content.every((p: any) => p.type === 'text' && typeof p.text === 'string')))) throw new HistoryUnavailable('Pi history unsupported message');
    }
    if (m.role === 'toolResult' && m.content.some((p: any) => p.type !== 'text' || typeof p.text !== 'string')) throw new HistoryUnavailable('Pi history unsupported tool content');
  }
  if (calls.size || messages.at(-1)?.role !== 'assistant' || messages.at(-1)?.stopReason !== 'stop') throw new HistoryUnavailable('Pi history is not a completed turn');
}

export function restore(value: any, sha256: string): AgentMessage[] {
  if (value?.version !== 1 || digest(value.messages) !== sha256) throw new HistoryUnavailable('Pi history checksum or version mismatch');
  validate(value.messages);
  // Re-project when reading too, rejecting provider-private or unknown injected fields.
  const clean = snapshot(value.messages);
  if (clean.sha256 !== sha256) throw new HistoryUnavailable('Pi history contains unsupported fields');
  return clean.messages;
}
