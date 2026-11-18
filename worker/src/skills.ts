import { createHmac } from 'node:crypto';
import type { AgentTool } from '@earendil-works/pi-agent-core';
import { validateControlUrl, type EvidenceChannel } from './evidence.js';

export interface SkillCard {
  skill_id: string; version_id: string; package_hash: string; name: string; description: string;
}
type ActiveSkill = SkillCard & { body: string; files: string[] };

export function skillChannel(channel: EvidenceChannel) {
  const base = validateControlUrl(channel.controlUrl);
  const signature = createHmac('sha256', channel.token)
    .update(`skills:v1:${channel.requestId}:${channel.taskId}:${channel.workerId}:${channel.contextId}`).digest('hex');
  async function call(action: 'catalog' | 'load' | 'resource', args: Record<string, unknown> = {}, signal?: AbortSignal) {
    await channel.ensureActive();
    const response = await fetch(`${base}/api/internal/delegations/${channel.requestId}/skills`, {
      method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${signature}`, 'Content-Type': 'application/json' },
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(action === 'catalog' ? 3000 : 20000)])
        : AbortSignal.timeout(action === 'catalog' ? 3000 : 20000),
      body: JSON.stringify({ action, ...args, task_id: channel.taskId, worker_id: channel.workerId,
        context_id: channel.contextId }),
    });
    if (!response.ok) throw new Error(`Skill controller rejected ${action} (${response.status})`);
    const raw = await response.text();
    if (Buffer.byteLength(raw) > 2 * 1024 * 1024) throw new Error('Skill response exceeds model view limit');
    await channel.ensureActive();
    return JSON.parse(raw);
  }
  return { call, async catalog(): Promise<SkillCard[]> {
    const result = await call('catalog');
    return result.skills;
  } };
}

export function workerSkillTools(channel: ReturnType<typeof skillChannel>, catalog: SkillCard[],
                                 runScript?: (id: string, args: Record<string, unknown>, signal?: AbortSignal) => Promise<any>) {
  const active = new Map<string, ActiveSkill>();
  const known = new Map(catalog.map(item => [item.skill_id, item]));
  const result = (value: unknown) => ({ content: [{ type: 'text' as const, text: JSON.stringify(value) }], details: value });
  const tools: AgentTool[] = [{
    name: 'search_skills', label: 'Search Skills',
    description: 'Search the task-fixed global Skill directory by name or description. Metadata only; use load_skill to activate instructions.',
    parameters: { type: 'object', properties: { query: { type: 'string' }, cursor: { type: 'integer', minimum: 0 } }, additionalProperties: false },
    async execute(_id, params) {
      const input = params as { query?: string; cursor?: number };
      const words = (input.query ?? '').toLowerCase().split(/\s+/).filter(Boolean);
      const matches = catalog.filter(item => words.every(word => `${item.name} ${item.description}`.toLowerCase().includes(word)));
      const offset = Math.max(0, input.cursor ?? 0);
      return result({ skills: matches.slice(offset, offset + 20), total: matches.length,
        next_cursor: offset + 20 < matches.length ? offset + 20 : null });
    },
  }, {
    name: 'load_skill', label: 'Load Skill',
    description: 'Load one task-fixed Skill version into this Worker context. This does not affect Main or execute scripts.',
    parameters: { type: 'object', properties: { skill_id: { type: 'string' } }, required: ['skill_id'], additionalProperties: false },
    async execute(_id, params, signal) {
      const skillId = String((params as { skill_id: string }).skill_id);
      const item = known.get(skillId);
      if (!item) throw new Error('Skill not in task catalog');
      if (active.has(skillId)) return result({ loaded: true, already_active: true, skill_id: skillId, version_id: item.version_id });
      const value = await channel.call('load', { skill_id: skillId, version_id: item.version_id }, signal) as ActiveSkill;
      if (value.package_hash !== item.package_hash) throw new Error('Skill package hash changed');
      if (Buffer.byteLength(value.body, 'utf8') + [...active.values()].reduce((size, loaded) => size + Buffer.byteLength(loaded.body, 'utf8'), 0) > 64000)
        throw new Error('Active Skill context exceeds Worker budget; unload another Skill');
      active.set(skillId, { ...item, body: value.body, files: value.files });
      return result({ loaded: true, skill_id: skillId, version_id: item.version_id,
        package_hash: item.package_hash, files: value.files, note: 'Instructions apply at next model request; no script was executed.' });
    },
  }, {
    name: 'read_skill_resource', label: 'Read Skill resource',
    description: 'Read a file from an active Skill by relative path. References are fetched only when needed.',
    parameters: { type: 'object', properties: { skill_id: { type: 'string' }, path: { type: 'string' },
      offset: { type: 'integer', minimum: 0 } }, required: ['skill_id', 'path'], additionalProperties: false },
    async execute(_id, params, signal) {
      const input = params as { skill_id: string; path: string; offset?: number };
      const item = active.get(input.skill_id);
      if (!item) throw new Error('Skill is not active in this Worker');
      return result(await channel.call('resource', { skill_id: input.skill_id, version_id: item.version_id,
        path: input.path, offset: input.offset ?? 0 }, signal));
    },
  }, {
    name: 'unload_skill', label: 'Unload Skill',
    description: 'Remove an active Skill from future Worker model requests without deleting files or prior messages.',
    parameters: { type: 'object', properties: { skill_id: { type: 'string' } }, required: ['skill_id'], additionalProperties: false },
    async execute(_id, params) {
      const skillId = String((params as { skill_id: string }).skill_id);
      const unloaded = active.delete(skillId);
      return result({ skill_id: skillId, unloaded });
    },
  }];
  if (runScript) tools.push({
    name: 'run_skill_script', label: 'Run Skill script in Kali',
    description: 'Execute scripts/ from an active Skill version through the existing Kali Executor and task scope. No human approval.',
    parameters: { type: 'object', properties: { skill_id: { type: 'string' }, path: { type: 'string' },
      argv: { type: 'array', items: { type: 'string' } }, stdin_json: { type: 'string' },
      execution_timeout_seconds: { type: 'integer', minimum: 1 } }, required: ['skill_id', 'path'], additionalProperties: false },
    async execute(id, params, signal) {
      const input = params as { skill_id: string; path: string; argv?: string[]; stdin_json?: string; execution_timeout_seconds?: number };
      const item = active.get(input.skill_id);
      if (!item) throw new Error('Skill is not active in this Worker');
      if (!input.path.startsWith('scripts/') || !item.files.includes(input.path)) throw new Error('Script is not in active Skill');
      return runScript(id, { ...input, version_id: item.version_id }, signal);
    },
  });
  const context = () => [...active.values()].sort((a, b) => a.name.localeCompare(b.name))
    .map(item => `【Worker 已加载 Skill ${item.name}；version=${item.version_id}；hash=${item.package_hash}】\n${item.body}\n资源按需读取；脚本仅通过 Kali Executor 执行。`)
    .join('\n\n');
  return { tools, context, active };
}
