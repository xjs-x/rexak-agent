import { useCallback, useEffect, useState } from "react";
import { api, type SkillItem, type SkillPage, type SkillResource } from "./api/client";

export function SkillLibrary({sessionId, projectId, selected, onSelected}: {
  sessionId: string; projectId: string; selected: string[]; onSelected: (ids: string[]) => void;
}) {
  const [page, setPage] = useState<SkillPage | null>(null);
  const [query, setQuery] = useState("");
  const [directory, setDirectory] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [versions, setVersions] = useState<SkillItem[]>([]);
  const [preview, setPreview] = useState<SkillResource | null>(null);
  const [opened, setOpened] = useState<SkillItem | null>(null);
  const [fileList, setFileList] = useState<string[]>([]);
  const [nextFiles, setNextFiles] = useState<number | null>(null);
  const [active, setActive] = useState<Record<string, unknown>[]>([]);
  const refresh = useCallback(async () => {
    setPage(await api.skills(query));
    if (sessionId) setActive((await api.activeSkills(sessionId)).active ?? []);
  }, [query, sessionId]);
  useEffect(() => { void refresh().catch((e: Error) => setNotice(e.message)); }, [refresh]);
  async function action(fn: () => Promise<unknown>, reload = true) {
    setBusy(true); setNotice("");
    try { await fn(); if (reload) await refresh(); }
    catch (e) { setNotice(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function upload(file: File) {
    if (file.size > 32 * 1024 * 1024) throw new Error("ZIP 文件超过 32 MiB");
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    await api.importSkill({zip_base64: btoa(binary)});
  }
  async function open(item: SkillItem) {
    setOpened(item);
    setVersions((await api.skillVersions(item.skill_id)).skills);
    const resource = await api.skillResource(item.skill_id, item.version_id);
    setPreview(resource); setFileList(resource.files ?? []); setNextFiles(resource.next_file_offset ?? null);
  }
  return <div className="stack skill-library">
    <p>这里仅接入你提供的外部方法论，原有工作流程和内置 methods 保持独立。将含 SKILL.md 的目录放入 <code>{page?.root || "配置的技能目录"}</code> 后刷新，或导入 ZIP。</p>
    <div className="skill-actions"><input aria-label="搜索技能" value={query} onChange={e => setQuery(e.target.value)} placeholder="按名称或中文描述搜索" />
      <button disabled={busy} onClick={() => void action(async () => { const result = await api.rescanSkills(); setNotice((result.errors ?? []).join("；") || "技能目录已刷新；新版本供后续运行使用。"); })}>刷新目录</button>
      <label>导入 ZIP <input aria-label="导入技能 ZIP" type="file" accept=".zip" disabled={busy} onChange={e => { const file = e.target.files?.[0]; if (file) void action(() => upload(file)); e.target.value = ""; }} /></label></div>
    <div className="skill-actions"><input aria-label="技能目录路径" value={directory} onChange={e => setDirectory(e.target.value)} placeholder="已配置根目录内的完整技能目录路径" />
      <button disabled={busy || !directory.trim()} onClick={() => void action(() => api.importSkill({directory}))}>导入目录</button></div>
    {notice && <p role="status">{notice}</p>}
    {page?.total === 0 && <p>尚未接入外部 Skill。当前任务继续使用原有工作流程。</p>}
    <p>已选 {selected.length} 项，将随下一条消息明确加载。正在运行的任务保留原目录和活跃版本。</p>
    {page?.skills.map(item => <article className="panel" key={item.skill_id}>
      <div className="panel-head"><label><input type="checkbox" checked={selected.includes(item.skill_id)} disabled={!item.enabled && !selected.includes(item.skill_id)}
        onChange={e => onSelected(e.target.checked ? [...selected, item.skill_id] : selected.filter(id => id !== item.skill_id))} /> {item.name}</label>
        <span>{item.enabled ? "已启用" : "已停用"} · {item.source}</span></div>
      <p>{item.description}</p><p className="fineprint">版本 {item.label || item.package_hash.slice(0, 12)} · {item.package_hash}</p>
      {(item.diagnostics ?? []).map((value, index) => <p key={index}>{value}</p>)}
      <div className="skill-actions"><button disabled={busy} onClick={() => void action(() => open(item), false)}>版本与文件</button>
        <button disabled={busy} onClick={() => void action(() => api.updateSkill(item.skill_id, {enabled: !item.enabled}))}>{item.enabled ? "停用" : "启用"}</button>
        {projectId && <><button disabled={busy} onClick={() => void action(() => api.updateSkill(item.skill_id, {project_ids: (item.project_ids ?? []).length ? [] : [projectId]}))}>{(item.project_ids ?? []).length ? "全部项目可见" : "仅当前项目可见"}</button>
          <button disabled={busy} onClick={() => void action(() => api.updateSkill(item.skill_id, {default_for_projects: (item.default_for_projects ?? []).includes(projectId) ? (item.default_for_projects ?? []).filter(id => id !== projectId) : [...(item.default_for_projects ?? []), projectId]}))}>{(item.default_for_projects ?? []).includes(projectId) ? "取消项目默认" : "设为项目默认"}</button></>}
      </div></article>)}
    {page?.next_cursor && <button disabled={busy} onClick={() => void action(async () => { const next = await api.skills(query, Number(page.next_cursor)); setPage({...next, skills: [...page.skills, ...next.skills]}); }, false)}>加载更多</button>}
    {opened && <section className="panel"><h3>{opened.name} 文件与版本</h3>
      <select aria-label="查看技能版本" value={opened.version_id} onChange={e => { const item = versions.find(v => v.version_id === e.target.value); if (item) void action(() => open(item), false); }}>
        {versions.map(v => <option key={v.version_id} value={v.version_id}>{v.label || v.package_hash.slice(0, 12)}</option>)}</select>
      <button disabled={busy} onClick={() => void action(() => api.updateSkill(opened.skill_id, {version_id: opened.version_id}))}>后续运行使用此版本</button>
      <a href={`/api/skills/${opened.skill_id}/versions/${opened.version_id}/export`} download>导出 ZIP</a>
      <div className="skill-actions">{fileList.map(path => <button key={path} disabled={busy} onClick={() => void action(async () => setPreview(await api.skillResource(opened.skill_id, opened.version_id, path)), false)}>{path}</button>)}</div>
      {nextFiles != null && <button disabled={busy} onClick={() => void action(async () => {
        const more = await api.skillResource(opened.skill_id, opened.version_id, "SKILL.md", 0, nextFiles);
        setFileList([...fileList, ...(more.files ?? [])]); setNextFiles(more.next_file_offset ?? null);
      }, false)}>更多文件</button>}
      <p><code>{preview?.path}</code></p>
      <pre className="skill-preview">{preview?.text ?? "二进制资源可通过 ZIP 导出。"}</pre>
      {preview?.next_offset != null && <button disabled={busy} onClick={() => void action(async () => setPreview(await api.skillResource(opened.skill_id, opened.version_id, preview.path, preview.next_offset!)), false)}>下一段</button>}
    </section>}
    <section className="panel"><h3>当前任务活跃指导</h3>{active.length ? active.map(ref => <p key={String(ref.skill_id)}>{String(ref.skill_id)} · {String(ref.package_hash).slice(0, 12)}</p>) : <p>暂无已加载技能。此处显示 checkpoint 活跃版本，不代表脚本正在运行。</p>}</section>
  </div>;
}
