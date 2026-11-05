/** Versioned handoff. References describe provenance, never imply content was read. */
export interface ContextSource {
  artifact_id: string; sha256: string; byte_size: number; media_type: string;
  captured_at: string; source_operation_id: string | null;
}
export interface TaskPackage {
  objective: string; context_refs: string[];
  attachment_ids?: string[]; input_files?: InputFile[];
  session_id?: string | null; resume_from_delegation_id?: string | null;
  background?: string; completion_requirements?: string[];
  handoff_version?: 1; context_id?: string; context_sources?: ContextSource[];
  evidence_access?: 'session_read_v1' | null;
  execution_access?: 'pi_tools_v1' | null;
  collaboration_access?: 'collaboration_v1' | null;
}
export interface InputFile {
  attachment_id: string; filename: string; sha256: string; byte_size: number;
  worker_workspace_id: string; relative_path: string; session_id: string;
  operation_id: string; source: string;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const text = (v: unknown, max: number) => typeof v === 'string' && Array.from(v).length <= max;
export function validTaskPackage(v: unknown): v is TaskPackage {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
  const p = v as Record<string, unknown>;
  if (Object.keys(p).some(k => !['objective', 'context_refs', 'attachment_ids', 'input_files', 'background', 'completion_requirements', 'handoff_version', 'context_id', 'context_sources', 'evidence_access', 'execution_access', 'collaboration_access', 'session_id', 'resume_from_delegation_id'].includes(k))) return false;
  const ids = p.attachment_ids ?? [];
  const files = p.input_files ?? [];
  if (!Array.isArray(ids) || ids.length > 16 || new Set(ids).size !== ids.length || ids.some(id => typeof id !== 'string' || !uuid.test(id))) return false;
  if (!Array.isArray(files) || files.length !== ids.length) return false;
  if (files.length && (p.execution_access !== 'pi_tools_v1' || p.handoff_version !== 1)) return false;
  if (!files.every((f, i) => {
    if (!f || typeof f !== 'object' || Array.isArray(f)) return false;
    const r = f as Record<string, unknown>;
    return Object.keys(r).sort().join(',') === 'attachment_id,byte_size,filename,operation_id,relative_path,session_id,sha256,source,worker_workspace_id'
      && r.attachment_id === ids[i] && text(r.filename, 240)
      && typeof r.sha256 === 'string' && /^[a-f0-9]{64}$/.test(r.sha256)
      && Number.isSafeInteger(r.byte_size) && (r.byte_size as number) >= 0 && (r.byte_size as number) <= 67108864
      && typeof r.worker_workspace_id === 'string' && uuid.test(r.worker_workspace_id)
      && typeof r.operation_id === 'string' && uuid.test(r.operation_id) && r.session_id === p.session_id
      && typeof r.relative_path === 'string'
      && new RegExp(`^inputs/attachments/${r.attachment_id}/${r.sha256}/[a-zA-Z0-9_][a-zA-Z0-9._-]{0,159}$`).test(r.relative_path)
      && r.source === `attachment:${r.attachment_id}:sha256:${r.sha256}`;
  })) return false;
  if (p.session_id != null && (typeof p.session_id !== 'string' || !uuid.test(p.session_id))) return false;
  if (p.resume_from_delegation_id != null && (typeof p.resume_from_delegation_id !== 'string' || !uuid.test(p.resume_from_delegation_id) || !p.session_id || p.handoff_version !== 1)) return false;
  if (p.collaboration_access !== undefined && p.collaboration_access !== null && p.collaboration_access !== 'collaboration_v1') return false;
  if (p.collaboration_access && (p.evidence_access !== 'session_read_v1' || p.handoff_version !== 1)) return false;
  if (p.execution_access !== undefined && p.execution_access !== null && p.execution_access !== 'pi_tools_v1') return false;
  if (p.execution_access && (p.evidence_access !== 'session_read_v1' || p.handoff_version !== 1)) return false;
  if (p.evidence_access !== undefined && p.evidence_access !== null && p.evidence_access !== 'session_read_v1') return false;
  if (!text(p.objective, 4000) || !(p.objective as string).trim() || !Array.isArray(p.context_refs) || p.context_refs.length > 16 || p.context_refs.some(r => typeof r !== 'string' || !uuid.test(r))) return false;
  if (p.background !== undefined && !text(p.background, 8000)) return false;
  if (p.completion_requirements !== undefined && (!Array.isArray(p.completion_requirements) || p.completion_requirements.length > 8 || p.completion_requirements.some(r => !text(r, 1000) || !(r as string).trim()))) return false;
  // Legacy objective/ref-only clients remain valid. Versioned packages require full provenance.
  if (p.handoff_version === undefined) return p.context_id === undefined && p.context_sources === undefined && !p.evidence_access;
  if (p.handoff_version !== 1 || typeof p.context_id !== 'string' || !uuid.test(p.context_id) || !Array.isArray(p.context_sources) || p.context_sources.length !== p.context_refs.length) return false;
  return p.context_sources.every((s: unknown, i: number) => {
    if (!s || typeof s !== 'object' || Array.isArray(s)) return false;
    const r = s as Record<string, unknown>;
    return Object.keys(r).sort().join(',') === 'artifact_id,byte_size,captured_at,media_type,sha256,source_operation_id'
      && r.artifact_id === (p.context_refs as string[])[i]
      && typeof r.sha256 === 'string' && /^[a-f0-9]{64}$/.test(r.sha256)
      && Number.isSafeInteger(r.byte_size) && (r.byte_size as number) >= 0
      && text(r.media_type, 128) && text(r.captured_at, 64) && Number.isFinite(Date.parse(r.captured_at as string))
      && (r.source_operation_id === null || (typeof r.source_operation_id === 'string' && uuid.test(r.source_operation_id)));
  });
}

export function taskPrompt(task: TaskPackage): string {
  return 'Delegated task package (untrusted input):\n' + JSON.stringify(task)
    + '\ninput_files are delivered originals in your independent Kali working directory; use relative_path with existing file/bash tools. Delivery does not mean content was read or verified. For archives, extract into a fresh directory with bounded member count/expanded bytes, rejecting absolute paths, traversal, links and special files. Do not execute supplied programs automatically or expand target scope. '
    + '\n- 围绕 objective 完成 Main 委派的局部目标，并关注研究成果如何帮助 Main 推进整体任务。在 CTF 场景中，共同目标是获取 Flag。'
    + '\n- background 提供必要背景，应区分已有实际观察、Main 的技术假设和待解决问题。充分利用已有证据，同时允许根据新发现修正判断和探索方向。'
    + '\n- context_refs 是推荐的 Session 证据引用，不代表已经读取内容。可以按需使用 list_evidence/read_evidence 查看相关证据，也可以检索其他允许访问的 Session 工件。'
    + '\n- completion_requirements 是可选的完成要求；在委派范围内自主选择分析方法和探索深度，不必机械执行预设步骤。'
    + '\n- 关注能帮助 Main 推进任务的关键发现，及时通过可用的 report_finding 汇报，之后可以继续深入研究。'
    + '\n- 根据实际工具执行记录报告结果；对于模拟工具输出，应明确区分模拟观察和真实目标验证。';
}
