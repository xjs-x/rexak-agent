import { useEffect, useState, type ReactNode } from "react";
import {
  api,
  type ArtifactPreview,
  type ModelSettings,
  type ReportRead,
  type RunItem,
  type SessionItem,
  type ToolActivityDetail,
  type ToolActivityItem,
  type WeaknessDetail,
} from "../api/client";
import { ModelConfiguration } from "../ModelConfiguration";
import { SkillLibrary } from "../SkillLibrary";
import { Icon } from "./Icon";
import {
  formatTime,
  responseOutcomeLabel,
  shortId,
  weaknessKindLabel,
  weaknessStatusLabel,
} from "./labels";
import type { DeleteResult } from "./useWorkbench";

export function BaseModal({
  title,
  sub,
  onClose,
  children,
  actions,
  wide,
}: {
  title: string;
  sub?: string;
  onClose: () => void;
  children: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="rk-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`rk-modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="rk-modal-head">
          <div style={{ flex: 1 }}>
            <h3>{title}</h3>
            {sub && <p className="sub">{sub}</p>}
          </div>
          <button className="icon-btn" title="关闭" onClick={onClose}>
            <Icon name="close" />
          </button>
        </div>
        <div className="rk-modal-body">{children}</div>
        {actions && <div className="rk-modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

export function NewProjectDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (name: string, description: string) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const ok = await onCreate(name.trim(), description.trim());
      if (ok) onClose();
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <BaseModal
      title="新建项目"
      sub="同一项目的任务、弱点与报告会归在一起。"
      onClose={onClose}
      actions={
        <>
          <button className="mini-btn" onClick={onClose} disabled={busy}>取消</button>
          <button className="mini-btn strong" onClick={() => void submit()} disabled={busy || !name.trim()}>
            {busy ? "创建中…" : "创建项目"}
          </button>
        </>
      }
    >
      <label className="rk-field">
        <span>项目名称</span>
        <input className="rk-input" value={name} autoFocus onChange={(event) => setName(event.target.value)} placeholder="例如：内网靶场复测" />
      </label>
      <label className="rk-field">
        <span>目标描述（可选）</span>
        <textarea className="rk-textarea" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="一句话说明这个项目要做什么" />
      </label>
      {error && <div className="drawer-error">{error}</div>}
    </BaseModal>
  );
}

export function DeleteSessionDialog({
  target,
  onClose,
  onConfirm,
}: {
  target: SessionItem;
  onClose: () => void;
  onConfirm: (terminate: boolean) => Promise<DeleteResult>;
}) {
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState<DeleteResult | null>(null);
  const [error, setError] = useState("");

  const run = async (terminate: boolean) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await onConfirm(terminate);
      if (result.ok) onClose();
      else if (result.blocked) setBlocked(result);
      else setError(result.message || "删除失败");
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <BaseModal
      title="删除任务？"
      sub={target.title}
      onClose={onClose}
      actions={
        blocked ? (
          <>
            <button className="mini-btn" onClick={onClose} disabled={busy}>保留</button>
            <button className="mini-btn strong" onClick={() => void run(true)} disabled={busy}>
              {busy ? "终止中…" : "终止资源并删除"}
            </button>
          </>
        ) : (
          <>
            <button className="mini-btn" onClick={onClose} disabled={busy}>保留</button>
            <button className="mini-btn strong" onClick={() => void run(false)} disabled={busy}>
              {busy ? "删除中…" : "删除任务"}
            </button>
          </>
        )
      }
    >
      <p className="meta">任务会从列表移除；历史审计记录保留。若仍有存活的执行资源，后端会拦下删除并列出资源。</p>
      {blocked && (
        <>
          <div className="warn" style={{ marginTop: 10 }}>{blocked.message}</div>
          {!!blocked.resources?.length && (
            <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 12, color: "#5a5a63" }}>
              {blocked.resources.map((item) => <li key={item}>{item}</li>)}
            </ul>
          )}
          <p className="meta">“终止资源并删除”会先请求关闭全部资源；未确认关闭的资源仍会如实显示。</p>
        </>
      )}
      {error && <div className="drawer-error" style={{ marginTop: 10 }}>{error}</div>}
    </BaseModal>
  );
}

export function RecoveryDialog({
  run,
  onClose,
  onConfirm,
}: {
  run: RunItem;
  onClose: () => void;
  onConfirm: () => Promise<boolean>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <BaseModal
      title="核对后恢复"
      sub={`run ${shortId(run.id)} · ${run.status}`}
      onClose={onClose}
      actions={
        <>
          <button className="mini-btn" onClick={onClose} disabled={busy}>暂不恢复</button>
          <button
            className="mini-btn strong"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setError("");
              void onConfirm()
                .then((ok) => { if (ok) onClose(); })
                .catch((cause) => setError(String(cause)))
                .finally(() => setBusy(false));
            }}
          >
            {busy ? "提交中…" : "确认并继续"}
          </button>
        </>
      }
    >
      <p className="meta">{run.stop_reason || "上一次请求的状态未能完全确认。"}</p>
      <div className="warn" style={{ marginTop: 10 }}>
        恢复前请核对工具结果与可能产生的费用。确认后会显式确认未知操作并继续当前任务，
        不会自动重做未知的工具操作。
      </div>
      {error && <div className="drawer-error" style={{ marginTop: 10 }}>{error}</div>}
    </BaseModal>
  );
}

export function ArtifactDialog({ artifactId, onClose }: { artifactId: string; onClose: () => void }) {
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void api
      .artifact(artifactId)
      .then((value) => active && setPreview(value))
      .catch((cause) => active && setError(String(cause instanceof Error ? cause.message : cause)));
    return () => {
      active = false;
    };
  }, [artifactId]);
  return (
    <BaseModal title="证据工件" sub={`artifact ${shortId(artifactId, 12)}`} onClose={onClose} wide
      actions={<button className="mini-btn" onClick={onClose}>关闭</button>}>
      {error && <div className="drawer-error">{error}</div>}
      {!preview && !error && <div className="drawer-loading">读取工件…</div>}
      {preview && (
        <>
          <p className="meta">{preview.media_type} · {preview.byte_size.toLocaleString()} 字节 · sha256 {preview.sha256.slice(0, 16)}…</p>
          <pre className="artifact-pre">{preview.preview}</pre>
          <p className="meta">{preview.note}</p>
        </>
      )}
    </BaseModal>
  );
}

export function ReportDialog({
  sessionId,
  version,
  onClose,
}: {
  sessionId: string;
  version: number;
  onClose: () => void;
}) {
  const [report, setReport] = useState<ReportRead | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setLoading(true);
    void api
      .report(sessionId, version)
      .then(async (value) => {
        if (!active) return;
        setReport(value);
        if (value.markdown_artifact_id) {
          const artifact = await api.artifact(value.markdown_artifact_id);
          if (active) setText(artifact.preview);
        }
      })
      .catch((cause) => active && setError(String(cause instanceof Error ? cause.message : cause)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [sessionId, version]);
  return (
    <BaseModal
      title={`报告 v${version}`}
      sub={report ? `${report.status}${report.is_historical ? " · 历史版本" : ""} · ${formatTime(report.created_at)}` : "读取中…"}
      onClose={onClose}
      wide
      actions={
        <>
          <a className="mini-btn" href={api.reportExportUrl(sessionId, version, "markdown")} download>导出 Markdown</a>
          <a className="mini-btn" href={api.reportExportUrl(sessionId, version, "html")} download>导出 HTML</a>
          <button className="mini-btn" onClick={onClose}>关闭</button>
        </>
      }
    >
      {error && <div className="drawer-error">{error}</div>}
      {loading && <div className="drawer-loading">读取报告…</div>}
      {!loading && !error && !text && <div className="drawer-empty">报告正文工件不存在或不可读取。</div>}
      {text && <pre className="report-pre">{text}</pre>}
      <p className="meta">导出由后端直接返回附件；界面不渲染 HTML，避免把报告内容当成可执行页面。</p>
    </BaseModal>
  );
}

export function WeaknessDialog({
  sessionId,
  weaknessId,
  onClose,
  onOpenArtifact,
}: {
  sessionId: string;
  weaknessId: string;
  onClose: () => void;
  onOpenArtifact: (id: string) => void;
}) {
  const [detail, setDetail] = useState<WeaknessDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void api
      .weakness(sessionId, weaknessId)
      .then((value) => active && setDetail(value))
      .catch((cause) => active && setError(String(cause instanceof Error ? cause.message : cause)));
    return () => {
      active = false;
    };
  }, [sessionId, weaknessId]);
  return (
    <BaseModal title="弱点详情" sub={`weakness ${shortId(weaknessId, 12)}`} onClose={onClose} wide
      actions={<button className="mini-btn" onClick={onClose}>关闭</button>}>
      {error && <div className="drawer-error">{error}</div>}
      {!detail && !error && <div className="drawer-loading">读取弱点详情…</div>}
      {detail && (
        <>
          <h4 style={{ margin: "0 0 4px" }}>{detail.weakness.title}</h4>
          <p className="meta">
            {weaknessStatusLabel(detail.weakness.status)} · {weaknessKindLabel(detail.weakness.weakness_kind)} · 第 {detail.weakness.revision ?? 1} 版
            {detail.weakness.verification_id ? " · 有验证记录" : " · 无验证记录"}
          </p>
          <p style={{ fontSize: 13, lineHeight: 1.7 }}>{detail.weakness.description || "暂无说明"}</p>
          <h4>提供的条件</h4>
          {detail.weakness.provided_conditions?.length ? (
            <ul style={{ fontSize: 12.5, lineHeight: 1.7 }}>
              {detail.weakness.provided_conditions.map((condition) => (
                <li key={condition.key}><code>{condition.key}</code> {condition.statement}{condition.limitations ? `（限制：${condition.limitations}）` : ""}</li>
              ))}
            </ul>
          ) : <p className="meta">尚未记录条件。</p>}
          <h4>关系</h4>
          {detail.relations.length ? detail.relations.map((relation) => (
            <div className="d-row" key={relation.link_id}>
              <span className="k">{relation.role}</span>
              <span className="v">线索 {shortId(relation.lead_id, 10)} · {relation.relation_state}{relation.condition_key ? ` · ${relation.condition_key}` : ""}</span>
            </div>
          )) : <p className="meta">暂无显式关系；这不代表它已经影响后续行动。</p>}
          <h4>验证记录</h4>
          {detail.verifications.length ? detail.verifications.map((verification) => (
            <div className="d-row" key={verification.id}>
              <span className="k">{verification.conclusion}</span>
              <span className="v">{verification.method_version} · {verification.performed_by} · {formatTime(verification.created_at)}{verification.limitations ? ` · ${verification.limitations}` : ""}</span>
            </div>
          )) : <p className="meta">还没有验证记录；候选不能升级为已确认。</p>}
          <h4>证据</h4>
          {detail.weakness.evidence_refs?.length ? (
            <div className="drawer-actions">
              {detail.weakness.evidence_refs.map((id) => (
                <button className="mini-btn" key={id} onClick={() => onOpenArtifact(id)}>工件 {shortId(id)}</button>
              ))}
            </div>
          ) : <p className="meta">未记录证据工件。</p>}
          <p className="meta">{detail.note}</p>
        </>
      )}
    </BaseModal>
  );
}

export function ToolDialog({
  detail,
  item,
  onClose,
}: {
  detail: ToolActivityDetail;
  item: ToolActivityItem;
  onClose: () => void;
}) {
  return (
    <BaseModal
      title={`工具操作 · ${item.tool_name}`}
      sub={`${item.status} · ${formatTime(item.started_at)}`}
      onClose={onClose}
      wide
      actions={<button className="mini-btn" onClick={onClose}>关闭</button>}
    >
      <h4>请求</h4>
      <pre className="artifact-pre">{detail.request_text || "（未记录请求）"}</pre>
      <h4>结果</h4>
      <pre className="artifact-pre">{detail.result_text || "（未记录结果）"}</pre>
      <p className="meta">{detail.note}</p>
      <p className="meta">
        operation {item.operation_id ? shortId(item.operation_id, 12) : "未记录"}
        {item.job_id ? ` · job ${shortId(item.job_id, 12)}` : ""}
        {item.job_state ? ` · ${item.job_state}` : ""}
        {item.sync_status && item.sync_status !== "in_sync" ? " · 状态同步待核对" : ""}
      </p>
    </BaseModal>
  );
}

export function ModelDialog({
  settings,
  onSaved,
  onClose,
}: {
  settings: ModelSettings | null;
  onSaved: (settings: ModelSettings) => void;
  onClose: () => void;
}) {
  return (
    <BaseModal title="模型设置" sub="Key 只在本机保存；控制端不会把它下发到执行岛。" onClose={onClose} wide
      actions={<button className="mini-btn" onClick={onClose}>关闭</button>}>
      <ModelConfiguration settings={settings} onSaved={onSaved} />
    </BaseModal>
  );
}

export function SkillsDialog({
  sessionId,
  projectId,
  onClose,
  onToast,
}: {
  sessionId: string;
  projectId: string;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  return (
    <BaseModal title="技能库" sub="当前阶段后端拒绝 skill_refs；这里只用于导入、启停和查看版本。" onClose={onClose} wide
      actions={<button className="mini-btn" onClick={onClose}>关闭</button>}>
      <SkillLibrary
        sessionId={sessionId}
        projectId={projectId}
        selected={[]}
        onSelected={() => onToast("当前阶段后端暂不启用技能注入；技能只用于查看与管理。")}
        note="当前阶段后端拒绝 skill_refs：这里只用于导入、启停和查看版本，不会随消息注入。"
      />
    </BaseModal>
  );
}

export function SessionOutcome({ outcome }: { outcome: string | null | undefined }) {
  const label = responseOutcomeLabel(outcome);
  return label ? <div className="chat-notice">{label}</div> : null;
}
