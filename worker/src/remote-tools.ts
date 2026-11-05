import { createHmac } from 'node:crypto';
import type { AgentTool } from '@earendil-works/pi-agent-core';
import { setTimeout as delay } from 'node:timers/promises';
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, createBashToolDefinition,
  createGrepToolDefinition, createFindToolDefinition, truncateHead, truncateLine } from '../vendor/pi-tools/index.js';
import { validateControlUrl, type EvidenceChannel } from './evidence.js';

const factories = { read: createReadToolDefinition, bash: createBashToolDefinition, edit: createEditToolDefinition,
  write: createWriteToolDefinition, grep: createGrepToolDefinition, find: createFindToolDefinition };
type Name = keyof typeof factories;
export function remoteTools(channel: EvidenceChannel, abortAgent: () => void) {
  let uncertain = false;
  const current = new Map<string, { digest: string; pending: Promise<any> }>();
  const outputRefs = new Map<string, any>();
  let outputRefsTruncated = false;
  const base = validateControlUrl(channel.controlUrl);
  const signature = createHmac('sha256', channel.token).update(`tools:v1:${channel.requestId}:${channel.taskId}:${channel.workerId}:${channel.contextId}`).digest('hex');
  async function rpc(tool: Name, id: string, phase: string, args: object = {}, extra: object = {}, signal?: AbortSignal): Promise<any> {
    let response: Response;
    try { response = await fetch(`${base}/api/internal/delegations/${channel.requestId}/tools`, {
      method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${signature}`, 'Content-Type': 'application/json' },
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000),
      body: JSON.stringify({ ...extra, tool, tool_call_id: id, phase, arguments: args,
        task_id: channel.taskId, worker_id: channel.workerId, context_id: channel.contextId }),
    }); } catch (error) { if (!signal?.aborted) { uncertain = true; abortAgent(); } throw error; }
    if (!response.ok) {
      if (response.status >= 500) { uncertain = true; abortAgent(); }
      throw new Error(`Worker controller rejected ${phase} (${response.status})`);
    }
    try {
    const reader = response.body!.getReader();
    const chunks: Uint8Array[] = []; let size = 0;
    while (true) { const part = await reader.read(); if (part.done) break; size += part.value.byteLength;
      if (size > 8 * 1024 * 1024) { await reader.cancel(); throw new Error('Worker response too large'); } chunks.push(part.value); }
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
    if (value.uncertain) { uncertain = true; abortAgent(); throw new Error(`Executor outcome unknown; do not repeat operation ${value.operation_id}`); }
    return value;
    } catch (error) { uncertain = true; abortAgent(); throw error; }
  }
  async function settle(cancel: boolean, reason: 'failure' | 'user_cancel' = 'user_cancel') {
    try { const value = await rpc('bash', '__settle__', 'settle', { cancel, reason }); return value.settled === true; }
    catch { return false; }
  }
  const tools = (Object.keys(factories) as Name[]).map(name => {
    const template = factories[name]('/', { operations: {}, exposeSessionEnvironment: false });
    template.description = template.description.replace('If truncated, full output is saved to a temp file.', 'Full output remains in Executor logs and Artifact references, never Node temporary files.');
    if (name === 'read') template.description += ' Remote adapter currently supports UTF-8 text files up to 4 MiB; image decoding is disabled.';
    return { ...template, async execute(id: string, params: unknown, signal?: AbortSignal, onUpdate?: any) {
      const digest = JSON.stringify([name, canonical(params)]);
      const active = current.get(id);
      if (active) {
        if (active.digest !== digest) throw new Error('ToolCall ID conflicts with running tool or arguments');
        return active.pending;
      }
      const pending = (async () => {
        if (uncertain) throw new Error('Worker has unresolved Executor operations; no new effects permitted');
        await channel.ensureActive();
        let step = 0;
        const versions = new Map<string, string>();
        const artifacts: string[] = [];
        let externalTruncation = false;
        const begin = await rpc(name, id, 'begin', params as object, {}, signal);
        if (begin.cached) return begin.cached;
        async function operation(action: string, args: Record<string, unknown>) {
          await channel.ensureActive();
          let value = await rpc(name, id, 'step', { action, ...args }, { step: ++step }, signal);
          while (value.pending) { await delay(150, undefined, { signal }); value = await rpc(name, id, 'poll', {}, { operation_id: value.operation_id }, signal); }
          if (value.artifact_id) artifacts.push(value.artifact_id);
          for (const ref of value.output_refs ?? []) {
            outputRefs.set(ref.artifact_id, ref);
            if (outputRefs.size > 32) { outputRefs.delete(outputRefs.keys().next().value!); outputRefsTruncated = true; }
          }
          if (value.error || value.ok === false) throw new Error(String(value.error));
          return value.data;
        }
        const fileOps = {
          access: async (path: string) => { await operation('access', { path }); },
          readFile: async (path: string) => {
            const value = await operation('read', { path }); const raw = Buffer.from(value.data_base64, 'base64');
            new TextDecoder('utf-8', { fatal: true }).decode(raw);
            versions.set(path, value.version); return raw;
          },
          writeFile: async (path: string, content: string) => { await operation('write', { path, content,
            ...(name === 'edit' ? { expected_version: versions.get(path) } : {}) }); },
          mkdir: async (path: string) => { await operation('mkdir', { path }); },
          exists: async (path: string) => (await operation('exists', { path })).exists,
          glob: async (pattern: string, path: string, opts: { limit: number }) => {
            const data = await operation('glob', { path, pattern, limit: opts.limit });
            externalTruncation ||= data.truncated;
            return data.lines;
          },
          detectImageMimeType: async () => null,
          exec: async (command: string, _cwd: string, opts: { onData: (data: Buffer) => void; timeout?: number }) => {
            const value = await operation('bash', { command, timeout: opts.timeout === undefined ? null : Math.ceil(opts.timeout) });
            externalTruncation = value.truncated;
            opts.onData(Buffer.from(value.output)); return { exitCode: value.exit_code };
          },
        };
        const tool = factories[name](begin.workspace, { operations: fileOps, autoResizeImages: false, exposeSessionEnvironment: false });
        if (name === 'grep') tool.execute = async (_id, args: any) => {
          const data = await operation('grep', { ...args, path: args.path || begin.workspace });
          const text = data.lines.map((line: string) => truncateLine(line).text).join('\n');
          const clipped = truncateHead(text);
          return { content: [{ type: 'text', text: (clipped.content || 'No matches found') + (data.truncated ? '\n[Remote search result limit reached]' : '') }], details: { truncation: clipped.truncated ? clipped : undefined, resultLimitReached: data.truncated ? args.limit ?? 100 : undefined } };
        };
        let result: any;
        try {
          result = await tool.execute(id, params, signal, onUpdate);
        } catch (error) {
          result = { content: [{ type: 'text', text: error instanceof Error ? error.message : 'Remote tool failure' }], isError: true };
          if (signal?.aborted || uncertain) {
            const confirmed = await settle(true, 'failure'); if (!confirmed) uncertain = true;
            throw error;
          }
        }
        const ownRefs = [...outputRefs.values()].filter(ref => artifacts.includes(ref.artifact_id));
        result.details = { ...result.details, executor_artifact_ids: artifacts, output_refs: ownRefs, executor_output_truncated: externalTruncation };
        if (ownRefs.length) result.content.push({ type: 'text', text: '[Executor output sources]\n' + ownRefs.map(ref =>
          `operation=${ref.operation_id} artifact=${ref.artifact_id} ${ref.stream} stored=[${ref.stored_offset},${ref.next_offset})/${ref.observed_size} truncated=${ref.truncated} decode=${ref.decode_status}`).join('\n') });
        if (externalTruncation && !result.content.some((part: any) => part.type === 'text' && part.text.includes('[Remote output incomplete]'))) {
          result.content.push({ type: 'text', text: '[Remote output incomplete] Captured ranges and original bytes are referenced by output_refs.' });
        }
        // No local full-output files: complete bytes remain in Executor logs/CAS.
        await rpc(name, id, 'finish', {}, { result }, signal);
        return result;
      })();
      current.set(id, { digest, pending });
      try { return await pending; }
      catch (error) { abortAgent(); throw error; }
      finally { current.delete(id); }
    } } as AgentTool;
  });
  return { tools, settle, outputRefs, get outputRefsTruncated() { return outputRefsTruncated; }, get uncertain() { return uncertain; } };
}

function canonical(value: any): any {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}
