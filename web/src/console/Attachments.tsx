import { useEffect, useRef, useState } from "react";
import { api, type Attachment, type AttachmentSlice } from "../api/client";

export type AttachmentSend = (text: string, ids?: string[], session?: string, key?: string) => Promise<boolean>;
type Pending = { key: string; file?: File; name: string; size: number; type: string;
  progress: number; state: "uploading" | "ready" | "failed"; error?: string; attachment?: Attachment };

export function useAttachments(sessionId: string, prepare?: () => Promise<string>, enabled = true) {
  const [files, setFiles] = useState<Pending[]>([]);
  const [recoveryError, setRecoveryError] = useState("");
  const filesRef = useRef(files);
  filesRef.current = files;
  const sessionRef = useRef<Promise<string> | null>(null);
  const controllers = useRef(new Map<string, AbortController>());
  const generation = useRef(0);
  const [capabilities, setCapabilities] = useState<Awaited<ReturnType<typeof api.attachmentCapabilities>> | null>(null);
  const capabilitiesPromise = useRef<ReturnType<typeof api.attachmentCapabilities> | null>(null);

  useEffect(() => {
    let active = true;
    if (enabled) {
      capabilitiesPromise.current = api.attachmentCapabilities();
      void capabilitiesPromise.current.then((value) => { if (active) setCapabilities(value); })
        .catch((error) => { if (active) { capabilitiesPromise.current = null; setRecoveryError(`读取附件限制失败：${String(error)}`); } });
    }
    return () => { active = false; };
  }, [enabled]);

  useEffect(() => {
    generation.current++;
    sessionRef.current = sessionId ? Promise.resolve(sessionId) : null;
    setFiles([]);
    setRecoveryError("");
    const version = generation.current;
    if (sessionId && enabled) void api.temporaryAttachments(sessionId).then((page) => {
      if (version !== generation.current) return;
      setFiles((current) => [...current, ...page.attachments.filter((a) => a.status === "temporary")
        .filter((a) => !current.some((p) => p.attachment?.attachment_id === a.attachment_id))
        .slice(0, Math.max(0, (capabilities?.max_per_message ?? 5) - current.length)).map((a): Pending => ({ key: crypto.randomUUID(),
          name: a.filename, size: a.size, type: a.content_type, progress: 100, state: "ready", attachment: a }))]);
    }).catch((error) => {
      if (version === generation.current) setRecoveryError(`恢复临时附件失败：${String(error)}`);
    });
    return () => {
      generation.current++;
      for (const controller of controllers.current.values()) controller.abort();
      controllers.current.clear();
    };
  }, [sessionId, enabled]);

  const patch = (key: string, update: Partial<Pending>) => setFiles((current) =>
    current.map((item) => item.key === key ? { ...item, ...update } : item));

  const upload = async (item: Pending) => {
    if (!item.file) return;
    const version = generation.current;
    const controller = new AbortController();
    controllers.current.set(item.key, controller);
    patch(item.key, { state: "uploading", progress: 0, error: undefined });
    try {
      if (!capabilitiesPromise.current) capabilitiesPromise.current = api.attachmentCapabilities();
      const limits = await capabilitiesPromise.current;
      if (version !== generation.current || controller.signal.aborted) return;
      setCapabilities(limits);
      if (filesRef.current.length > limits.max_per_message) throw new Error(`单消息最多选择 ${limits.max_per_message} 个附件，请移除多余文件后重试。`);
      const binary = /\.(exe|dll|elf|sys|so|bin)$/i.test(item.name) || !item.name.includes(".");
      const maximum = binary ? limits.binary_max_bytes : limits.max_bytes;
      if (item.size > maximum) throw new Error(`文件超过上传上限 ${formatSize(maximum)}；二进制上限仅适用于真实 PE/ELF。`);
      if (!sessionRef.current) {
        if (!prepare) throw new Error("当前无法创建附件会话");
        sessionRef.current = prepare().catch((error) => { sessionRef.current = null; throw error; });
      }
      const target = await sessionRef.current;
      if (version !== generation.current || controller.signal.aborted) return;
      const attachment = await api.uploadAttachment(target, item.file, item.key, controller.signal,
        (progress) => patch(item.key, { progress }));
      if (version !== generation.current) return;
      if (controller.signal.aborted) {
        void api.removeAttachment(target, attachment.attachment_id).catch((error) =>
          setRecoveryError(`取消后的临时附件保留至到期：${String(error)}`));
        return;
      }
      patch(item.key, { attachment, state: "ready", progress: 100 });
    } catch (error) {
      if (version === generation.current && !controller.signal.aborted)
        patch(item.key, { state: "failed", error: error instanceof Error ? error.message : String(error) });
    } finally { controllers.current.delete(item.key); }
  };

  const add = (selected: File[]) => {
    if (!enabled) throw new Error("当前后端尚未加载附件版本，请等待服务切换完成");
    const maximum = capabilities?.max_per_message ?? 5;
    const slots = maximum - filesRef.current.length;
    if (selected.length > slots) throw new Error(`单消息最多选择 ${maximum} 个附件`);
    const added = selected.map((file): Pending => ({ key: crypto.randomUUID(), file,
      name: file.name, size: file.size, type: file.type || "未知类型", progress: 0, state: "uploading" }));
    filesRef.current = [...filesRef.current, ...added];
    setFiles(filesRef.current);
    for (const item of added) void upload(item);
  };

  const remove = async (item: Pending) => {
    controllers.current.get(item.key)?.abort();
    if (item.attachment && sessionRef.current) {
      await api.removeAttachment(await sessionRef.current, item.attachment.attachment_id);
    }
    setFiles((current) => current.filter((p) => p.key !== item.key));
  };

  return { files, add, remove, retry: upload, recoveryError, capabilities,
    blocked: files.some((p) => p.state !== "ready"),
    async payload() { return { ids: files.map((p) => p.attachment!.attachment_id),
      session: files.length && sessionRef.current ? await sessionRef.current : sessionId }; },
    clear() { setFiles([]); },
  };
}

export function PendingAttachments({ queue, onError, disabled }: {
  queue: ReturnType<typeof useAttachments>; onError: (message: string) => void; disabled?: boolean;
}) {
  return <div className="attachment-list">
    {queue.recoveryError && <small className="chat-error">{queue.recoveryError}</small>}
    {queue.files.map((item) => <div className="attachment-card" key={item.key}>
      <strong>{item.name}</strong><small>{item.type} · {formatSize(item.size)}</small>
      {item.state === "uploading" && <><progress value={item.progress} max={100} />
        <small>{item.progress === 100 ? "上传完成，服务端解析中…" : `上传 ${item.progress}%`}</small></>}
      {item.state === "ready" && <small>已保存 · {item.attachment?.metadata?.note as string || "可按需读取"}</small>}
      {item.error && <small className="chat-error">{item.error}</small>}
      {item.state === "failed" && <button type="button" disabled={disabled} onClick={() => void queue.retry(item)}>重试上传</button>}
      <button type="button" disabled={disabled} onClick={() => void queue.remove(item).catch((e) => onError(String(e)))}>移除</button>
    </div>)}
  </div>;
}

export function AttachmentCards({ sessionId, attachments }: { sessionId: string; attachments: Attachment[] }) {
  const [selected, setSelected] = useState<Attachment | null>(null);
  const [slice, setSlice] = useState<AttachmentSlice | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const requestId = useRef(0);
  const [analyses, setAnalyses] = useState<Awaited<ReturnType<typeof api.attachmentAnalyses>>>([]);
  const [analysesLoaded, setAnalysesLoaded] = useState(false);
  useEffect(() => {
    setAnalyses([]);
    setAnalysesLoaded(false);
    if (!selected || !isBinary(selected)) return;
    let active = true;
    const refresh = () => void api.attachmentAnalyses(sessionId, selected.attachment_id)
      .then((value) => { if (active) { setAnalyses(value); setAnalysesLoaded(true); } })
      .catch((e) => { if (active) setError(`读取分析状态失败：${String(e)}`); });
    refresh();
    const timer = window.setInterval(refresh, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [selected, sessionId]);
  const load = async (attachment: Attachment, cursor: Record<string, number> = {}) => {
    const currentRequest = ++requestId.current;
    setLoading(true); setError("");
    try {
      const next = await api.attachmentText(sessionId, attachment.attachment_id, cursor);
      if (currentRequest === requestId.current) setSlice(next);
    } catch (e) { if (currentRequest === requestId.current) setError(String(e)); }
    finally { if (currentRequest === requestId.current) setLoading(false); }
  };
  return <>
    <div className="attachment-list">{attachments.map((a) => <button type="button" className="attachment-card"
      key={a.attachment_id} onClick={() => { requestId.current++; setSelected(a); setSlice(null); setError(""); setLoading(false); if (a.readable) void load(a); }}>
      <strong>{a.filename}</strong><small>{a.content_type} · {formatSize(a.size)}</small>
      <small>{isBinary(a) ? "二进制资料 · 点击查看静态分析状态" : `${a.parse_status} · 用户资料`}</small>
    </button>)}</div>
    {selected && <div className="attachment-overlay" onClick={() => setSelected(null)}>
      <section className="attachment-preview" role="dialog" aria-modal="true" aria-label="附件预览" onClick={(e) => e.stopPropagation()}>
        <button type="button" onClick={() => setSelected(null)}>关闭</button>
        <h3>{selected.filename}</h3>
        <p>{selected.content_type} · {formatSize(selected.size)} · {selected.parse_status}</p>
        <p>用户提供的资料，不代表已验证的事实或测试授权。</p>
        <small className="mono">SHA256: {selected.sha256}</small>
        <p>{String(selected.metadata?.note ?? "")}</p>
        {isBinary(selected) && <div><p>{!analysesLoaded ? "正在读取静态分析状态…" : analyses.length ? "静态分析记录" : "未分析；Agent 可按需调用二进制静态分析工具。"}</p>
          {analyses.map((a) => <div key={a.analysis_id}><small>{a.kind} · {analysisLabel(a.status)} · {a.analysis_id}</small>
            <pre>{JSON.stringify(a.summary, null, 2)}</pre>{a.error_type && <p>{a.error_type}</p>}</div>)}
          <p>反汇编与引用可在工具活动中查看；不提供反编译或运行文件。</p></div>}
        <a href={`/api/sessions/${sessionId}/attachments/${selected.attachment_id}/content`} download>下载原文件</a>
        {selected.content_type.startsWith("image/") && selected.parse_status === "metadata_only" &&
          <img alt={selected.filename} src={`/api/sessions/${sessionId}/attachments/${selected.attachment_id}/content?preview=true`} />}
        {!selected.readable && !selected.content_type.startsWith("image/") && <p>此文件暂不支持正文预览。</p>}
        {loading && <p>读取中…</p>}{error && <p className="chat-error">{error}</p>}
        {slice && <><p>页 {slice.page} · 行 {slice.start_line}–{slice.end_line} · {slice.status}</p>
          <pre>{slice.text || slice.note}</pre><small className="mono">{slice.reference}</small>
          {slice.next_cursor && <button type="button" disabled={loading}
            onClick={() => void load(selected, slice.next_cursor!)}>下一段</button>}</>}
      </section>
    </div>}
  </>;
}

export function formatSize(size: number) { return size < 1024 ? `${size} B` : size < 1048576 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1048576).toFixed(1)} MiB`; }
function isBinary(a: Attachment) { return a.metadata?.binary_candidate === true || /\.(exe|dll|elf|sys|so|bin)$/i.test(a.filename); }
function analysisLabel(status: string) { return ({ pending: "分析中", ready: "已分析", failed: "分析失败", interrupted: "已中断，可重试", unsupported: "此格式不支持深入分析" } as Record<string, string>)[status] ?? status; }
