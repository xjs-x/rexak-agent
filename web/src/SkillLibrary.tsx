import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiRequestError, api, type SkillItem, type SkillPage, type SkillResource } from "./api/client";
import { Icon } from "./console/Icon";
import "./skill-library.css";

type View = "library" | "active";
type Form = "create" | "directory" | null;
type Notice = { text: string; error?: boolean };
type FileEditor = { resource: SkillResource; original: string | null; text: string | null };
type Detail = {
  current: SkillItem;
  version: SkillItem;
  versions: SkillItem[];
  files: string[];
  nextFiles: number | null;
  editor: FileEditor;
};

const versionLabel = (item: SkillItem) => item.label || item.package_hash.slice(0, 12);
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

async function encodeFile(file: File) {
  if (file.size > 32 * 1024 * 1024) throw new Error("文件超过 32 MiB");
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

async function readFile(item: SkillItem, path: string): Promise<FileEditor> {
  const resource = await api.skillResource(item.skill_id, item.version_id, path);
  let part = resource;
  let content = part.text ?? null;
  while (content !== null && part.next_offset != null) {
    if (content.length > 1_000_000) throw new Error("文件超过在线编辑上限，请导出 ZIP 后查看。");
    part = await api.skillResource(item.skill_id, item.version_id, path, part.next_offset);
    content += part.text ?? "";
  }
  return { resource, original: content, text: content };
}

function EmptyState({ title, children }: { title: string; children: string }) {
  return <div className="sl-empty">
    <span className="sl-symbol"><Icon name="layers" size="lg" /></span>
    <strong>{title}</strong>
    <p>{children}</p>
  </div>;
}

export function SkillLibrary({ sessionId, sessionOnly = false, onPendingChanges }: {
  sessionId: string;
  sessionOnly?: boolean;
  onPendingChanges?: (pending: boolean) => void;
}) {
  const [view, setView] = useState<View>(sessionOnly ? "active" : "library");
  const [form, setForm] = useState<Form>(null);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState<SkillPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [active, setActive] = useState<Record<string, unknown>[] | null>(null);
  const [activeError, setActiveError] = useState("");
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [newBody, setNewBody] = useState("");
  const [directory, setDirectory] = useState("");
  const [newFile, setNewFile] = useState(false);
  const [path, setPath] = useState("");
  const [fileContent, setFileContent] = useState("");
  const [uploadFolder, setUploadFolder] = useState<"references" | "scripts">("references");
  const [archive, setArchive] = useState<SkillItem | null>(null);
  const [reload, setReload] = useState(0);
  const [conflict, setConflict] = useState(false);
  const [previousDraft, setPreviousDraft] = useState<{ path: string; text: string } | null>(null);
  const zipInput = useRef<HTMLInputElement>(null);
  const resourceInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (sessionOnly) return;
    let current = true;
    const timer = window.setTimeout(() => {
      setLoading(true);
      void api.skills(query.trim()).then(result => {
        if (current) setPage(result);
      }).catch(error => {
        if (current) setNotice({ text: errorText(error), error: true });
      }).finally(() => { if (current) setLoading(false); });
    }, query ? 160 : 0);
    return () => { current = false; window.clearTimeout(timer); };
  }, [query, reload, sessionOnly]);

  useEffect(() => {
    setActive(null);
    setActiveError("");
    if (!sessionId || view !== "active") return;
    let current = true;
    const refresh = () => void api.activeSkills(sessionId).then(result => {
      if (current) { setActive(result.active ?? []); setActiveError(""); }
    }).catch(error => { if (current) setActiveError(errorText(error)); });
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { current = false; window.clearInterval(timer); };
  }, [sessionId, view]);

  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setNotice(null);
    try { await fn(); }
    catch (error) { setNotice({ text: errorText(error), error: true }); }
    finally { setBusy(false); }
  }

  async function open(current: SkillItem, version = current, file = "SKILL.md") {
    const [editor, versions, index] = await Promise.all([
      readFile(version, file),
      api.skillVersions(current.skill_id),
      file === "SKILL.md" ? null : api.skillResource(current.skill_id, version.version_id),
    ]);
    const resource = index ?? editor.resource;
    setDetail({
      current, version, versions: versions.skills,
      files: resource.files ?? [], nextFiles: resource.next_file_offset ?? null, editor,
    });
    setNewFile(false);
  }

  const change = () => setReload(value => value + 1);

  function installed(item: SkillItem) {
    setForm(null);
    setQuery("");
    change();
    setNotice({ text: `${item.name} 已${item.enabled ? "更新" : "安装"}。${item.enabled ? "" : "可在列表中启用。"}` });
  }

  function create(event: FormEvent) {
    event.preventDefault();
    void action(async () => {
      installed(await api.createSkill({ name: newName.trim(), description: newDescription.trim(), body: newBody }));
      setNewName(""); setNewDescription(""); setNewBody("");
    });
  }

  function importDirectory(event: FormEvent) {
    event.preventDefault();
    void action(async () => {
      installed(await api.importSkill({ directory: directory.trim() }));
      setDirectory("");
    });
  }

  const dirty = detail != null && detail.editor.text !== detail.editor.original;
  const historical = detail != null && detail.current.version_id !== detail.version.version_id;
  const pending = dirty || Boolean(newName || newDescription || newBody || path || fileContent || previousDraft);
  useEffect(() => { onPendingChanges?.(pending); }, [pending, onPendingChanges]);

  async function saveFiles(current: SkillItem, changes: {
    text_files?: Record<string, string | null>;
    binary_files?: Record<string, string>;
  }, file: string) {
    let saved: SkillItem;
    try {
      saved = await api.editSkillFiles(current.skill_id, { ...changes, base_version_id: current.version_id });
    } catch (error) {
      if (error instanceof ApiRequestError && error.status === 409) setConflict(true);
      throw error;
    }
    await open(saved, saved, file);
    setConflict(false);
    change();
    setNotice({ text: `已保存版本 ${versionLabel(saved)}。` });
  }

  return <div className="skill-library" aria-busy={busy}>
    {!sessionOnly && <div className="sl-toolbar">
      <div className="sl-tabs" role="group" aria-label="技能库视图">
        <button type="button" aria-pressed={view === "library"} disabled={busy} onClick={() => setView("library")}>
          <Icon name="layers" />全局技能
        </button>
        <button type="button" aria-pressed={view === "active"} disabled={busy} onClick={() => setView("active")}>
          <Icon name="activity" />当前 Main
        </button>
      </div>
      {view === "library" && !detail && <div className="sl-actions">
        <button type="button" className="sl-button" disabled={busy} onClick={() => zipInput.current?.click()}>
          <Icon name="arrow-up" />导入 ZIP
        </button>
        <input ref={zipInput} hidden type="file" aria-label="导入技能 ZIP" accept=".zip" disabled={busy} onChange={event => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (file) void action(async () => installed(await api.importSkill({ zip_base64: await encodeFile(file) })));
        }} />
        <button type="button" className="sl-button sl-primary" disabled={busy} onClick={() => setForm(form === "create" ? null : "create")}>
          <Icon name="plus" />新建技能
        </button>
      </div>}
    </div>}

    {notice && <div className={`sl-notice${notice.error ? " is-error" : ""}`} role={notice.error ? "alert" : "status"}>
      <Icon name={notice.error ? "alert" : "check"} />{notice.text}
      <button type="button" className="sl-icon" aria-label="关闭提示" onClick={() => setNotice(null)}><Icon name="close" /></button>
    </div>}

    {view === "library" && !detail && <>
      {form === "create" && <form className="sl-form" onSubmit={create}>
        <h4>新建技能</h4>
        <div className="sl-fields">
          <label>名称<input required maxLength={64} value={newName} onChange={event => setNewName(event.target.value)} placeholder="例如 document-review" /></label>
          <label>使用场景<input required maxLength={1024} value={newDescription} onChange={event => setNewDescription(event.target.value)} placeholder="何时应该使用这个技能" /></label>
        </div>
        <label>SKILL.md 正文<textarea required rows={6} value={newBody} onChange={event => setNewBody(event.target.value)} placeholder="填写 Markdown 指导正文" /></label>
        <div className="sl-actions"><button type="button" className="sl-button" onClick={() => {
          setForm(null); setNewName(""); setNewDescription(""); setNewBody("");
        }}>取消</button>
          <button type="submit" className="sl-button sl-primary" disabled={busy}>创建技能</button></div>
      </form>}
      <div className="sl-searchbar">
        <label className="sl-search"><Icon name="search" /><input aria-label="搜索技能" type="search" value={query}
          onChange={event => setQuery(event.target.value)} placeholder="搜索技能名称或描述" /></label>
        <button type="button" className="sl-button" aria-expanded={form === "directory"} disabled={busy}
          onClick={() => setForm(form === "directory" ? null : "directory")}><Icon name="folder" />本地目录</button>
      </div>
      {form === "directory" && <form className="sl-form" onSubmit={importDirectory}>
        <h4>从本地目录导入</h4>
        {page?.root && <p>技能根目录 <code>{page.root}</code></p>}
        <label>技能目录路径<input required value={directory} onChange={event => setDirectory(event.target.value)}
          placeholder="输入根目录内含 SKILL.md 的子目录" /></label>
        <div className="sl-actions">
          <button type="button" className="sl-button" disabled={busy} onClick={() => void action(async () => {
            const result = await api.rescanSkills();
            change();
            setNotice({ text: result.errors?.join("；") || "目录已扫描。", error: Boolean(result.errors?.length) });
          })}><Icon name="refresh" />扫描根目录</button>
          <button type="submit" className="sl-button sl-primary" disabled={busy || !directory.trim()}>导入目录</button>
        </div>
      </form>}
      {loading ? <div className="sl-loading" role="status">正在读取技能…</div> : page?.total === 0 ? (
        query.trim() ? <EmptyState title="没有匹配的技能">试试其他名称或描述。</EmptyState>
          : <EmptyState title="技能库还是空的">新建一个技能，或导入包含 SKILL.md 的 ZIP 包。</EmptyState>
      ) : page && <>
        <div className="sl-list-head">技能 <span>{page.total}</span></div>
        <div className="sl-list">{page.skills.map(item => {
          const unavailable = item.diagnostics?.some(message => message.startsWith("resource_unavailable"));
          return <article className="sl-skill" key={item.skill_id}>
            <button className="sl-skill-info" type="button" aria-label={`查看 ${item.name} 的版本与文件`}
              disabled={busy} onClick={() => void action(() => open(item))}>
              <span className="sl-symbol"><Icon name="doc" /></span>
              <span><strong>{item.name}</strong><span className="sl-description">{item.description}</span>
                <span className="sl-version" title={item.package_hash}>{versionLabel(item)}</span></span>
              <Icon name="chev-r" size="sm" />
            </button>
            <div className="sl-skill-controls">
              <span className={`sl-status${unavailable ? " is-error" : item.enabled ? " is-enabled" : ""}`}>
                {unavailable ? "资源不可用" : item.enabled ? "已启用" : "未启用"}
              </span>
              <button type="button" className="sl-button" aria-label={`${item.enabled ? "停用" : "启用"} ${item.name}`}
                disabled={busy} onClick={() => void action(async () => {
                  await api.updateSkill(item.skill_id, { enabled: !item.enabled });
                  change();
                  setNotice({ text: `${item.name} 已${item.enabled ? "停用" : "启用"}。` });
                })}>{item.enabled ? "停用" : "启用"}</button>
              <button type="button" className="sl-icon sl-danger" aria-label={`移出 ${item.name}`} title="移出技能库"
                disabled={busy} onClick={() => setArchive(item)}><Icon name="trash" /></button>
            </div>
          </article>;
        })}</div>
        {page.next_cursor && <button type="button" className="sl-button sl-more" disabled={busy} onClick={() => void action(async () => {
          const next = await api.skills(query.trim(), Number(page.next_cursor));
          setPage({ ...next, skills: [...page.skills, ...next.skills] });
        })}>加载更多</button>}
      </>}
      {archive && <section className="sl-form sl-confirm" aria-label="确认移出技能">
        <h4>移出 {archive.name}？</h4><p>此技能将从全局库中移出，历史版本会保留。</p>
        <div className="sl-actions">
          <button type="button" className="sl-button" onClick={() => setArchive(null)}>取消</button>
          <button type="button" className="sl-button sl-danger" disabled={busy} onClick={() => void action(async () => {
            await api.archiveSkill(archive.skill_id); setArchive(null); change();
            setNotice({ text: `${archive.name} 已移出技能库。` });
          })}>确认移出</button>
        </div>
      </section>}
    </>}

    {view === "active" && (!sessionId
      ? <EmptyState title="尚未打开会话">进入会话后，这里会显示 Main 实际加载的技能。</EmptyState>
      : activeError ? <div className="sl-notice is-error" role="alert">读取 Main 状态失败：{activeError}</div>
      : active === null ? <div className="sl-loading" role="status">正在读取当前 Main…</div>
      : !active.length ? <EmptyState title="Main 暂未加载技能">在会话输入框输入 /skill 可选择要加载的技能。</EmptyState>
      : <div className="sl-list">{active.map(item => {
        const id = String(item.skill_id);
        const name = String(item.name ?? page?.skills.find(skill => skill.skill_id === id)?.name ?? id);
        const unavailable = item.status === "unavailable";
        return <article className="sl-active-row" key={id}>
          <span className="sl-symbol"><Icon name="spark" /></span>
          <span><strong>{name}</strong><span className="sl-version">{String(item.package_hash ?? "").slice(0, 12)}</span>
            {unavailable && <span className="sl-description">{String(item.reason ?? "固定版本不可用")}</span>}</span>
          <span className={`sl-status ${unavailable ? "is-error" : "is-enabled"}`}>{unavailable ? "不可用" : "已加载"}</span>
        </article>;
      })}</div>)}

    {view === "library" && detail && <>
      <div className="sl-detail-head">
        <button type="button" className="sl-button" disabled={busy || dirty || previousDraft != null}
          onClick={() => { setDetail(null); setConflict(false); }}>返回列表</button>
        <h4>{detail.version.name}</h4>
        <label className="sl-version-picker"><span>版本</span>
          <select aria-label="查看技能版本" disabled={busy || dirty} value={detail.version.version_id} onChange={event => {
            const version = detail.versions.find(item => item.version_id === event.target.value);
            if (version) void action(() => open(detail.current, version));
          }}>{detail.versions.map(item => <option key={item.version_id} value={item.version_id}>
            {versionLabel(item)}{item.version_id === detail.current.version_id ? " · 当前" : ""}</option>)}</select>
        </label>
        <a className="sl-button" href={`/api/skills/${detail.current.skill_id}/versions/${detail.version.version_id}/export`} download>导出 ZIP</a>
      </div>
      {conflict && <div className="sl-notice is-error">
        当前版本已更新。可加载最新版本后对照原草稿。
        <button type="button" className="sl-button" disabled={busy} onClick={() => void action(async () => {
          const latest = await api.skillDetail(detail.current.skill_id);
          const file = detail.editor.resource.path;
          // Keep the old draft separately so it does not overwrite changes in the latest version.
          const draft = dirty && detail.editor.text !== null ? { path: file, text: detail.editor.text } : null;
          try { await open(latest, latest, file); }
          catch (error) {
            if (!(error instanceof ApiRequestError) || !error.message.includes("skill_resource_not_found")) throw error;
            await open(latest);
          }
          if (draft) setPreviousDraft(draft);
          setConflict(false);
        })}>加载最新版本</button>
      </div>}
      {previousDraft && <details className="sl-form" open>
        <summary>对照原草稿 · {previousDraft.path}</summary>
        <textarea aria-label="原草稿" readOnly rows={5} value={previousDraft.text} />
        <button type="button" className="sl-button" onClick={() => setPreviousDraft(null)}>清除原草稿</button>
      </details>}
      {historical && <div className="sl-notice">
        正在查看历史版本。
        <button type="button" className="sl-button" disabled={busy} onClick={() => void action(async () => {
          await api.updateSkill(detail.current.skill_id, { version_id: detail.version.version_id });
          await open(detail.version); change(); setNotice({ text: "已将此版本设为当前版本。" });
        })}>设为当前版本</button>
      </div>}
      <div className="sl-editor">
        <nav aria-label="技能文件" className="sl-file-list">
          <div className="sl-list-head">文件</div>
          {detail.files.map(file => <button key={file} type="button" disabled={busy || dirty}
            aria-current={file === detail.editor.resource.path ? "true" : undefined} onClick={() => void action(async () => {
              const editor = await readFile(detail.version, file);
              setDetail({ ...detail, editor });
            })}><Icon name="file" size="sm" />{file}</button>)}
          {detail.nextFiles != null && <button type="button" disabled={busy} onClick={() => void action(async () => {
            const more = await api.skillResource(detail.current.skill_id, detail.version.version_id, "SKILL.md", 0, detail.nextFiles!);
            setDetail({ ...detail, files: [...detail.files, ...(more.files ?? [])], nextFiles: more.next_file_offset ?? null });
          })}>更多文件</button>}
        </nav>
        <section className="sl-file-main" aria-label="技能文件编辑器">
          <div className="sl-file-head"><code>{detail.editor.resource.path}</code>
            <span>{detail.editor.resource.byte_size.toLocaleString()} 字节</span></div>
          {detail.editor.text !== null ? <textarea aria-label="编辑 Skill 文件" spellCheck={false} rows={12}
            readOnly={historical || busy} value={detail.editor.text}
            onChange={event => setDetail({ ...detail, editor: { ...detail.editor, text: event.target.value } })} />
            : <p className="sl-loading">二进制资源可通过导出 ZIP 查看。</p>}
          {!historical && <div className="sl-file-footer">
            <button type="button" className="sl-button sl-primary" disabled={busy || !dirty}
              onClick={() => void action(() => saveFiles(detail.current,
                { text_files: { [detail.editor.resource.path]: detail.editor.text } }, detail.editor.resource.path))}>保存新版本</button>
            {dirty && <button type="button" className="sl-button" disabled={busy}
              onClick={() => setDetail({ ...detail, editor: { ...detail.editor, text: detail.editor.original } })}>放弃修改</button>}
            {!dirty && detail.editor.resource.path !== "SKILL.md" && <button type="button" className="sl-button sl-danger" disabled={busy}
              onClick={() => void action(() => saveFiles(detail.current, { text_files: { [detail.editor.resource.path]: null } }, "SKILL.md"))}>删除文件</button>}
          </div>}
        </section>
      </div>
      {!historical && <div className="sl-actions">
        <button type="button" className="sl-button" aria-expanded={newFile} disabled={busy || dirty} onClick={() => setNewFile(!newFile)}>
          <Icon name="plus" />新增文本文件
        </button>
        <div className="sl-upload">
          <select aria-label="上传目录" value={uploadFolder} disabled={busy || dirty} onChange={event =>
            setUploadFolder(event.target.value === "scripts" ? "scripts" : "references")}>
            <option value="references">references/</option>
            <option value="scripts">scripts/</option>
          </select>
          <button type="button" className="sl-button" disabled={busy || dirty} onClick={() => resourceInput.current?.click()}>
            <Icon name="arrow-up" />上传文件
          </button>
        </div>
        <input ref={resourceInput} type="file" hidden aria-label="上传资源文件" onChange={event => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          void action(async () => {
            const target = `${uploadFolder}/${file.name}`;
            await saveFiles(detail.current, { binary_files: { [target]: await encodeFile(file) } }, target);
          });
        }} />
      </div>}
      {newFile && !historical && <form className="sl-form" onSubmit={event => {
        event.preventDefault();
        if (!path.trim()) return;
        void action(async () => {
          await saveFiles(detail.current, { text_files: { [path.trim()]: fileContent } }, path.trim());
          setPath(""); setFileContent("");
        });
      }}>
        <h4>新增文本文件</h4>
        <label>文件路径<input required placeholder="references/notes.md" value={path} onChange={event => setPath(event.target.value)} /></label>
        <label>文件内容<textarea rows={4} value={fileContent} onChange={event => setFileContent(event.target.value)} /></label>
        <div className="sl-actions"><button type="button" className="sl-button" onClick={() => {
          setNewFile(false); setPath(""); setFileContent("");
        }}>取消</button>
          <button type="submit" className="sl-button sl-primary" disabled={busy || dirty}>添加到新版本</button></div>
      </form>}
    </>}
  </div>;
}
