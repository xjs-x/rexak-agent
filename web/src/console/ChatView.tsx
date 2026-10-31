import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AgentEvent,
  type ConversationEntry,
  type RunItem,
  type ToolActivityDetail,
  type ToolActivityItem,
  type UsageSummary,
  type WeaknessItem,
} from "../api/client";
import { Icon } from "./Icon";
import { AttachmentCards, PendingAttachments, useAttachments, formatSize, type AttachmentSend } from "./Attachments";
import { MarkdownText } from "./Markdown";
import type { ModelOption } from "./useWorkbench";
import {
  canStop,
  classifyTool,
  durationLabel,
  formatMessageTime,
  modeLabel,
  requestSummary,
  responseOutcomeLabel,
  runLabel,
  shortId,
  splitLines,
  toolStateLabel,
  toolStateTone,
  toolTitle,
  weaknessKindLabel,
  weaknessStatusLabel,
} from "./labels";

type TimelineItem =
  | { kind: "message"; entry: ConversationEntry }
  | { kind: "tools"; items: ToolActivityItem[] };

function buildTimeline(conversation: ConversationEntry[], tools: ToolActivityItem[]): TimelineItem[] {
  const ordered = [
    ...conversation.map((entry) => ({ kind: "message" as const, at: entry.occurred_at, entry })),
    ...tools.map((item) => ({ kind: "tool" as const, at: item.started_at, item })),
  ].sort((left, right) => Date.parse(left.at) - Date.parse(right.at));
  const timeline: TimelineItem[] = [];
  for (const item of ordered) {
    if (item.kind === "message") {
      timeline.push({ kind: "message", entry: item.entry });
      continue;
    }
    const previous = timeline.at(-1);
    if (previous?.kind === "tools" && previous.items[0]?.run_id === item.item.run_id) {
      previous.items.push(item.item);
    } else {
      timeline.push({ kind: "tools", items: [item.item] });
    }
  }
  return timeline;
}

/* ---------------- 输入框 ---------------- */
export function Composer({
  attachmentsEnabled = false,
  sessionId = "",
  onPrepareAttachments,
  placeholder,
  disabled,
  busy,
  scopeMode,
  models,
  modelKey,
  modelLocked,
  onModelChange,
  onConfigureModel,
  onScope,
  onSend,
  onStop,
  stopper,
  onToast,
  seed,
}: {
  attachmentsEnabled?: boolean;
  sessionId?: string;
  onPrepareAttachments?: () => Promise<string>;
  placeholder: string;
  disabled?: boolean;
  busy?: boolean;
  scopeMode: string | null;
  models: ModelOption[];
  modelKey: string;
  modelLocked: boolean;
  onModelChange: (key: string) => void;
  onConfigureModel: () => void;
  onScope: () => void;
  onSend: AttachmentSend;
  onStop?: () => void;
  stopper?: boolean;
  onToast: (message: string) => void;
  seed?: { text: string; nonce: number };
}) {
  const [value, setValue] = useState("");
  const [menu, setMenu] = useState(false);
  const [modelMenu, setModelMenu] = useState(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const queue = useAttachments(sessionId, onPrepareAttachments, attachmentsEnabled);
  const submitting = useRef(false);
  const pendingMessage = useRef<{ signature: string; key: string } | null>(null);
  const current = models.find((model) => model.key === modelKey) ?? models[0];
  const limits = queue.capabilities;
  const attachTitle = limits
    ? `添加文件 · 普通附件 ≤ ${formatSize(limits.max_bytes)}；真实 PE/ELF ≤ ${formatSize(limits.binary_max_bytes)}。只读静态分析，不执行。`
    : "添加文件";

  useEffect(() => {
    if (!seed?.text) return;
    setValue(seed.text);
    requestAnimationFrame(() => {
      textarea.current?.focus();
      if (textarea.current) {
        textarea.current.style.height = "auto";
        textarea.current.style.height = `${Math.min(textarea.current.scrollHeight, 150)}px`;
      }
    });
  }, [seed?.nonce, seed?.text]);

  const submit = async () => {
    const text = value.trim() || (queue.files.length ? "请分析附件。" : "");
    if (!text || busy || disabled || submitting.current || queue.blocked) return;
    submitting.current = true;
    try {
      const payload = await queue.payload();
      const signature = JSON.stringify([text, payload.ids, payload.session]);
      if (pendingMessage.current?.signature !== signature)
        pendingMessage.current = { signature, key: crypto.randomUUID() };
      const ok = await onSend(text, payload.ids, payload.session, pendingMessage.current.key);
      if (ok) {
        setValue(""); queue.clear(); pendingMessage.current = null;
        if (textarea.current) textarea.current.style.height = "auto";
      }
    } finally { submitting.current = false; }
  };

  const renderModel = (option: ModelOption) => (
    <button
      key={option.key}
      type="button"
      className={`mm-item${option.key === modelKey ? " active" : ""}`}
      disabled={modelLocked || !option.configured}
      onClick={() => {
        onModelChange(option.key);
        setModelMenu(false);
      }}
    >
      <span className="mm-main">
        <span className="mm-name">{option.model}</span>
        <span className="mm-provider">
          {option.providerLabel}
          {option.isDefault ? " · 默认" : ""}
        </span>
      </span>
      {!option.configured && <span className="mm-warn">未配置 Key</span>}
      {option.key === modelKey && <Icon name="check" size="sm" />}
    </button>
  );

  return (
    <form
      className="composer"
      onDragOver={(event) => { event.preventDefault(); }}
      onDrop={(event) => {
        event.preventDefault();
        if (busy || disabled) return;
        try { queue.add(Array.from(event.dataTransfer.files)); } catch (e) { onToast(String(e)); }
      }}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      {modelMenu && (
        <div className="model-menu" onMouseLeave={() => setModelMenu(false)}>
          <div className="mm-head">选择模型</div>
          {modelLocked ? (
            <div className="mm-lock">
              <Icon name="lock" size="sm" />
              当前运行阶段固定模型；阶段结束后可切换。
            </div>
          ) : (
            <div className="mm-hint">模型用于新阶段；暂停后继续沿用原模型。</div>
          )}
          <div className="mm-group">默认</div>
          {models.filter((model) => model.isDefault).map(renderModel)}
          <div className="mm-group">全部模型</div>
          {models.filter((model) => !model.isDefault).map(renderModel)}
          <button
            type="button"
            className="mm-config"
            onClick={() => {
              setModelMenu(false);
              onConfigureModel();
            }}
          >
            配置模型与 Key
          </button>
        </div>
      )}
      {menu && (
        <div className="attach-menu" role="menu" onMouseLeave={() => setMenu(false)}>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            disabled={busy || disabled || !attachmentsEnabled}
            onClick={() => {
              setMenu(false);
              fileInput.current?.click();
            }}
          >
            <span className="menu-ico"><Icon name="paperclip" size="sm" /></span>
            <span className="menu-main">
              <span className="menu-title">添加文件</span>
              <span className="menu-sub">
                {attachmentsEnabled
                  ? "从计算机上传 · 只读静态分析，不执行"
                  : "当前后端尚未加载附件版本；服务切换完成后开放上传。"}
              </span>
            </span>
          </button>
        </div>
      )}
      <input type="file" multiple hidden disabled={!attachmentsEnabled || busy || disabled} ref={fileInput} onChange={(event) => {
        try { queue.add(Array.from(event.target.files ?? [])); } catch (e) { onToast(String(e)); }
        event.target.value = "";
      }} />
      <PendingAttachments queue={queue} onError={onToast} disabled={busy || disabled} />
      <textarea
        ref={textarea}
        rows={1}
        value={value}
        disabled={disabled || busy}
        placeholder={placeholder}
        aria-label="输入指令"
        onChange={(event) => {
          setValue(event.target.value);
          event.target.style.height = "auto";
          event.target.style.height = `${Math.min(event.target.scrollHeight, 150)}px`;
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void submit();
          }
        }}
      />
      <div className="composer-bar">
        <button
          type="button"
          className="attach"
          aria-label="添加文件"
          title={attachTitle}
          onClick={() => {
            setModelMenu(false);
            setMenu((open) => !open);
          }}
        >
          <Icon name="plus" />
        </button>
        <button type="button" className="scope" onClick={onScope} title="查看当前任务范围">
          <Icon name="shield" size="sm" />
          {modeLabel(scopeMode)}
          <Icon name="chev-d" size="sm" />
        </button>
        <span className="spacer" />
        <button
          type="button"
          className={`model-chip${modelLocked ? " locked" : ""}`}
          onClick={() => {
            setMenu(false);
            setModelMenu((open) => !open);
          }}
          title={modelLocked ? "当前运行阶段固定模型" : "切换模型"}
        >
          {modelLocked && <Icon name="lock" size="sm" />}
          <span className="mc-name">{current?.model ?? "选择模型"}</span>
          {current && <span className="mc-provider">{current.providerLabel}</span>}
          <Icon name="chev-d" size="sm" />
        </button>
        {stopper && !value.trim() && !queue.files.length ? (
          <button
            type="button"
            className="send stop"
            disabled={busy}
            title="暂停当前运行；发送新指令即可继续"
            onClick={onStop}
          >
            <span className="stop-square" />
          </button>
        ) : (
          <button type="submit" className="send" disabled={(!value.trim() && !queue.files.length) || queue.blocked || busy || disabled} title="发送">
            <Icon name="arrow-up" />
          </button>
        )}
      </div>
    </form>
  );
}

/* ---------------- 消息正文 ---------------- */
function MessageBody({ sessionId, entry }: { sessionId: string; entry: ConversationEntry }) {
  const [body, setBody] = useState(entry.preview);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const identity = useRef(`${entry.source}:${entry.id}`);
  const outcome = responseOutcomeLabel(entry.response_outcome);

  useEffect(() => {
    identity.current = `${entry.source}:${entry.id}`;
    setBody(entry.preview);
    setNextOffset(null);
    setError("");
    setLoading(false);
  }, [entry.id, entry.preview, entry.source]);

  const load = async (offset = 0) => {
    if (!sessionId || loading) return;
    setLoading(true);
    setError("");
    try {
      const page = await api.conversationBody(sessionId, entry, offset);
      setBody((previous) => (offset && nextOffset !== null ? previous + page.text : page.text));
      setNextOffset(page.next_offset);
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setLoading(false);
    }
  };

  if (!entry.available) {
    return <div className="chat-notice">这段正文已不可读取（归档不可用或已清理）。</div>;
  }
  if (entry.role === "user") {
    return (
      <div className="msg user">
        <div className="user-wrap">
          <div className="bubble">
            <AttachmentCards sessionId={sessionId} attachments={entry.attachments ?? []} />
            {body || entry.preview}
            {entry.has_more && (
              <button className="mini-btn" style={{ marginTop: 6 }} disabled={loading} onClick={() => void load(0)}>
                {loading ? "读取中…" : "展开完整指令"}
              </button>
            )}
            {error && <p className="chat-error">{error}</p>}
          </div>
          <div className="msg-time">{formatMessageTime(entry.occurred_at)}</div>
        </div>
      </div>
    );
  }
  return (
    <div className="msg">
      <div className="body">
        {entry.role !== "assistant" && <p className="digest-note">角色：{entry.role}</p>}
        {outcome && <div className="chat-notice" style={{ marginBottom: 10 }}>{outcome}；以下仅为已收到的片段。</div>}
        <MarkdownText text={body || entry.preview} />
        {entry.has_more && (
          <button className="mini-btn" style={{ marginTop: 10 }} disabled={loading} onClick={() => void load(nextOffset ?? 0)}>
            {loading ? "读取中…" : nextOffset === null ? "展开完整正文" : "继续加载正文"}
          </button>
        )}
        {error && <p className="chat-error">{error}</p>}
      </div>
    </div>
  );
}

/* ---------------- 工具活动 ---------------- */
function ActivityGroup({
  sessionId,
  items,
  onOpenTool,
}: {
  sessionId: string;
  items: ToolActivityItem[];
  onOpenTool: (detail: ToolActivityDetail, item: ToolActivityItem) => void;
}) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, ToolActivityDetail | "loading" | "error">>({});

  const files = items.filter((item) => classifyTool(item.tool_name) === "edit").length;
  const commands = items.filter((item) => classifyTool(item.tool_name) === "command").length;
  const others = items.length - files - commands;
  const summary = [
    files ? `编辑了 ${files} 个文件` : "",
    commands ? `运行了 ${commands} 条命令` : "",
    others ? `调用了 ${others} 个工具` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const openDetail = async (item: ToolActivityItem) => {
    const key = `${item.run_id}:${item.tool_call_id}`;
    const next = expanded === key ? null : key;
    setExpanded(next);
    if (!next) return;
    const current = details[key];
    if (current && current !== "error") return;
    setDetails((previous) => ({ ...previous, [key]: "loading" }));
    try {
      const detail = await api.toolActivityDetail(sessionId, item);
      setDetails((previous) => ({ ...previous, [key]: detail }));
    } catch {
      setDetails((previous) => ({ ...previous, [key]: "error" }));
    }
  };

  return (
    <div className={`activity ${open ? "open" : ""}`}>
      <button className="activity-summary" onClick={() => setOpen((current) => !current)}>
        <Icon name="pencil" size="sm" />
        <span>{summary || `工具操作 · ${items.length} 项`}</span>
        <Icon name="chev-d" size="sm" className="chev" />
      </button>
      <div className="op-wrap">
        <div className="op-list">
          {items.map((item) => {
            const key = `${item.run_id}:${item.tool_call_id}`;
            const kind = classifyTool(item.tool_name);
            const detail = details[key];
            const tone = toolStateTone(item.status);
            const isOpen = expanded === key;
            return (
              <div className={`op-item ${isOpen ? "open" : ""}`} key={key}>
                <button className="op-row" onClick={() => void openDetail(item)}>
                  <span className="op-ic">
                    <Icon name={kind === "command" ? "terminal" : kind === "edit" ? "pencil" : "layers"} size="sm" />
                  </span>
                  <span>
                    {kind === "command"
                      ? "已运行命令"
                      : kind === "edit"
                        ? "已编辑文件"
                        : `已执行 ${toolTitle(item.tool_name)}`}
                  </span>
                  {tone !== "done" && <span className={`t-state ${tone}`}>{toolStateLabel(item.status)}</span>}
                  <span className="t-time">{item.finished_at ? durationLabel(item.started_at, item.finished_at) : "进行中"}</span>
                  <Icon name="chev-r" size="sm" className="chev2" />
                </button>
                {isOpen && (
                  <div className="op-detail">
                    {detail === "loading" || detail === undefined ? (
                      <div className="drawer-loading">读取操作详情…</div>
                    ) : detail === "error" ? (
                      <div className="drawer-error">操作详情读取失败。可以稍后重试，或到任务详情 → 实时活动核对。</div>
                    ) : (
                      <>
                        <div className="cmd-line">
                          <span className="p">{kind === "command" ? "$" : "›"}</span>
                          <span className="cmd-text">{requestSummary(detail.request_text, 400) || toolTitle(item.tool_name)}</span>
                          <span className="cmd-meta">
                            {toolStateLabel(item.status)}
                            {item.job_id ? ` · job ${shortId(item.job_id)}` : ""}
                          </span>
                        </div>
                        {detail.result_text && (
                          <OutBlock
                            text={detail.result_text}
                            file={kind === "edit"}
                            onOpen={() => onOpenTool(detail, item)}
                          />
                        )}
                        {detail.note && <p className="op-hint">{detail.note}</p>}
                        <p className="op-hint">
                          {item.operation_id ? `operation ${shortId(item.operation_id)}` : "operation 未记录"}
                          {item.sync_status && item.sync_status !== "in_sync" ? " · 状态同步待核对" : ""}
                        </p>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function OutBlock({ text, file, onOpen }: { text: string; file: boolean; onOpen: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const lines = splitLines(text, 400);
  const canExpand = lines.length > 1;
  return (
    <>
      <div className="out-wrap">
        <pre className={`out ${expanded ? "expanded" : ""}`}>{text}</pre>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        {canExpand && (
          <button className="out-toggle" onClick={() => setExpanded((current) => !current)}>
            {expanded ? (file ? "收起改动" : "收起结果") : file ? "展开改动" : `展开结果 · ${lines.length} 行`}
          </button>
        )}
        <button className="out-toggle" onClick={onOpen}>
          查看完整内容
        </button>
      </div>
    </>
  );
}

/* ---------------- 本轮弱点 ---------------- */
function WeaknessDigest({
  weaknesses,
  onOpen,
  onEvidence,
}: {
  weaknesses: WeaknessItem[];
  onOpen: (id: string) => void;
  onEvidence: (id: string) => void;
}) {
  const ordered = useMemo(
    () =>
      [...weaknesses]
        .sort((left, right) => {
          if (left.status === "confirmed" && right.status !== "confirmed") return -1;
          if (right.status === "confirmed" && left.status !== "confirmed") return 1;
          return (right.revision ?? 0) - (left.revision ?? 0);
        })
        .slice(0, 3),
    [weaknesses],
  );
  if (!ordered.length) return null;
  return (
    <div className="digest">
      <div className="digest-head">任务弱点记录</div>
      <p className="digest-note">来自弱点表，不是模型文本；候选不会自动计入已确认。</p>
      {ordered.map((item) => {
        const confirmed = item.status === "confirmed";
        const evidenceRef = item.evidence_refs?.[0];
        return (
          <article className={`weak-card ${confirmed ? "confirmed" : "candidate"}`} key={item.id}>
            <div className="weak-top">
              <span className="weak-title">{item.title}</span>
              <span className={`kind ${confirmed ? "conf" : "cand"}`}>
                <Icon name={confirmed ? "check" : "clock"} size="sm" />
                {confirmed ? "已确认" : weaknessStatusLabel(item.status)}
              </span>
            </div>
            <p className="weak-desc">{item.impact || "暂无说明"}</p>
            <div className="weak-meta">
              <span>{weaknessKindLabel(item.weakness_kind)}</span>
              <span>{item.category}</span>
              <span>第 {item.revision ?? 1} 版</span>
              <span>{item.condition_keys?.length ?? 0} 条条件</span>
              <span>{item.verification_id ? "有验证记录" : "无验证记录"}</span>
            </div>
            <div className="weak-foot">
              <span className="weak-note">
                {confirmed ? "确认由验证记录支撑。" : "候选不计入已确认；需要新的验证记录。"}
              </span>
              <button className="mini-btn" onClick={() => onOpen(item.id)}>查看详情</button>
              {evidenceRef && (
                <button className="mini-btn" onClick={() => onEvidence(evidenceRef)}>
                  查看证据
                </button>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}

/* ---------------- 真实状态卡 ---------------- */
function StateCard({
  run,
  inputRequired,
  tools,
  onControl,
  onAck,
  onOpenDrawer,
  onCreateReport,
  onCreateReportBusy,
}: {
  run: RunItem;
  inputRequired: AgentEvent | null;
  tools: ToolActivityItem[];
  onControl: (action: "pause" | "resume" | "cancel") => void;
  onAck: () => void;
  onOpenDrawer: (tab: string) => void;
  onCreateReport: () => void;
  onCreateReportBusy: boolean;
}) {
  const payload = (inputRequired?.payload ?? {}) as Record<string, unknown>;
  const pendingTools = tools.filter((item) =>
    ["preparing", "accepted", "received", "starting", "running"].includes(item.status),
  ).length;

  if (run.status === "waiting_tool" && run.model_response_saved) {
    return (
      <div className="state-card recover">
        <div className="sc-head">
          <span className="sc-ic"><Icon name="db" size="sm" /></span>
          <div>
            <h4>模型回复已保存，执行状态待核对</h4>
            <p>
              本轮模型文本已经落库，
              {pendingTools ? `但仍有 ${pendingTools} 个工具操作没有返回终态。` : "但仍有工具操作没有返回终态。"}
              这不能算任务成功，也不能据此判断远端没有副作用。
            </p>
          </div>
        </div>
        <div className="sc-actions">
          <button className="mini-btn" onClick={() => onOpenDrawer("activity")}>查看实时活动</button>
          <button className="mini-btn" onClick={() => onOpenDrawer("resources")}>查看执行资源</button>
        </div>
        <p className="sc-fine">等待工具结果期间不会派发新的模型请求。</p>
      </div>
    );
  }

  if (run.status === "waiting_user") {
    const scopeIssue = payload.reason === "scope_change_requires_explicit_apply";
    return (
      <div className="state-card ask">
        <div className="sc-head">
          <span className="sc-ic"><Icon name="alert" size="sm" /></span>
          <div>
            <h4>等待你的输入</h4>
            <p>
              {String(payload.message ?? "") ||
                run.stop_reason ||
                "运行停在等待输入的检查点，尚未派发新的操作。"}
            </p>
          </div>
        </div>
        <div className="sc-actions">
          <button className="mini-btn strong" onClick={() => onControl("resume")}>继续运行</button>
          {scopeIssue && <button className="mini-btn" onClick={() => onOpenDrawer("settings")}>查看当前范围</button>}
          <button className="mini-btn" onClick={() => onOpenDrawer("activity")}>查看活动</button>
        </div>
        <p className="sc-fine">
          {scopeIssue
            ? "范围变更只能通过显式范围接口应用；继续运行不会悄悄改范围。"
            : "也可以直接在下方补充指令；补充指令与“继续运行”是两种不同操作。"}
        </p>
      </div>
    );
  }

  if (run.status === "recovering") {
    return (
      <div className="state-card recover">
        <div className="sc-head">
          <span className="sc-ic"><Icon name="refresh" size="sm" /></span>
          <div>
            <h4>运行已中断，恢复前需要核对远端</h4>
            <p>{run.stop_reason || "上一次工具调用的结果未能确认。恢复不会自动重做未知操作。"}</p>
          </div>
        </div>
        <div className="sc-actions">
          <button className="mini-btn strong" onClick={onAck}>核对后继续</button>
          <button className="mini-btn" onClick={() => onOpenDrawer("resources")}>查看执行资源</button>
          <button className="mini-btn" onClick={() => onOpenDrawer("activity")}>查看活动</button>
        </div>
        <p className="sc-fine">“没查到”不等于“没有副作用”；确认继续会显式确认未知操作。</p>
      </div>
    );
  }

  if (run.status === "failed") {
    return (
      <div className="state-card fail">
        <div className="sc-head">
          <span className="sc-ic"><Icon name="alert" size="sm" /></span>
          <div>
            <h4>运行失败</h4>
            <p>{run.stop_reason || "运行中断，原因未记录。请查看实时活动与执行资源。"}</p>
          </div>
        </div>
        <div className="sc-actions">
          <button className="mini-btn strong" onClick={() => onControl("resume")}>从断点继续</button>
          <button className="mini-btn" onClick={() => onOpenDrawer("activity")}>查看活动</button>
        </div>
        <p className="sc-fine">恢复沿用已保存的检查点，不会重做已确认的操作。</p>
      </div>
    );
  }

  if (run.status === "completed") {
    return (
      <div className="state-card done">
        <div className="sc-head">
          <span className="sc-ic"><Icon name="check" size="sm" /></span>
          <div>
            <h4>本阶段已结束</h4>
            <p>
              {run.stop_reason ||
                `结束理由：${run.completion_reason || "未记录"}。已确认的弱点与未决项以真实记录为准。`}
            </p>
          </div>
        </div>
        <div className="sc-actions">
          <button className="mini-btn strong" disabled={onCreateReportBusy} onClick={onCreateReport}>
            {onCreateReportBusy ? "生成中…" : "生成报告版本"}
          </button>
          <button className="mini-btn" onClick={() => onOpenDrawer("reports")}>查看报告</button>
        </div>
        <p className="sc-fine">报告版本只在后端生成成功后出现；未决项会单独保留。</p>
      </div>
    );
  }

  if (run.status === "paused") {
    return (
      <div className="state-card">
        <div className="sc-head">
          <span className="sc-ic"><Icon name="pause" size="sm" /></span>
          <div>
            <h4>已暂停</h4>
            <p>{run.stop_reason || "运行已暂停；发送新指令或点击继续后才会推进。"}</p>
          </div>
        </div>
        <div className="sc-actions">
          <button className="mini-btn" onClick={() => onControl("cancel")}>取消本轮</button>
        </div>
        <p className="sc-fine">直接在下方发送一条新指令就会继续同一个 run；运行台也保留“继续”。</p>
      </div>
    );
  }

  return null;
}

/* ---------------- 对话主视图 ---------------- */
export function ChatView({
  attachmentsEnabled,
  onPrepareAttachments,
  sessionId,
  scopeMode,
  run,
  conversation,
  tools,
  assistantText,
  assistantStartedAt,
  modelPhase,
  inputRequired,
  weaknesses,
  usage,
  hasMoreMessages,
  hasMoreTools,
  loadingMore,
  busy,
  models,
  modelKey,
  modelLocked,
  onModelChange,
  onConfigureModel,
  onSend,
  onControl,
  onAck,
  onOpenDrawer,
  onOpenWeakness,
  onOpenEvidence,
  onOpenTool,
  onLoadOlderMessages,
  onLoadMoreTools,
  onToast,
  onCreateReport,
  onCreateReportBusy,
}: {
  attachmentsEnabled: boolean;
  onPrepareAttachments: () => Promise<string>;
  sessionId: string;
  scopeMode: string | null;
  run: RunItem | null;
  conversation: ConversationEntry[];
  tools: ToolActivityItem[];
  assistantText: string;
  assistantStartedAt: string | null;
  modelPhase: string | null;
  inputRequired: AgentEvent | null;
  weaknesses: WeaknessItem[];
  usage: UsageSummary | null;
  hasMoreMessages: boolean;
  hasMoreTools: boolean;
  loadingMore: boolean;
  busy: boolean;
  models: ModelOption[];
  modelKey: string;
  modelLocked: boolean;
  onModelChange: (key: string) => void;
  onConfigureModel: () => void;
  onSend: AttachmentSend;
  onControl: (action: "pause" | "resume" | "cancel") => void;
  onAck: () => void;
  onOpenDrawer: (tab: string) => void;
  onOpenWeakness: (id: string) => void;
  onOpenEvidence: (id: string) => void;
  onOpenTool: (detail: ToolActivityDetail, item: ToolActivityItem) => void;
  onLoadOlderMessages: () => void;
  onLoadMoreTools: () => void;
  onToast: (message: string) => void;
  onCreateReport: () => void;
  onCreateReportBusy: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const visibleTools = useMemo(() => {
    const runIds = new Set(conversation.map((entry) => entry.run_id).filter((id): id is string => !!id));
    if (run?.id) runIds.add(run.id);
    return tools.filter((item) => runIds.has(item.run_id));
  }, [conversation, run?.id, tools]);

  const timeline = useMemo(() => buildTimeline(conversation, visibleTools), [conversation, visibleTools]);

  // 一轮的“用时”取该轮最后一条消息/工具的时间，而不是第一条 assistant 的时间。
  const turnMeta = useMemo(() => {
    const result = new Map<number, string>();
    for (let index = 0; index < timeline.length; index += 1) {
      const item = timeline[index];
      if (!item || item.kind !== "message" || item.entry.role !== "user") continue;
      const startAt = item.entry.occurred_at;
      let endIndex = timeline.length - 1;
      for (let next = index + 1; next < timeline.length; next += 1) {
        const candidate = timeline[next];
        if (candidate?.kind === "message" && candidate.entry.role === "user") {
          endIndex = next - 1;
          break;
        }
      }
      let lastAt = startAt;
      for (let cursor = index + 1; cursor <= endIndex; cursor += 1) {
        const candidate = timeline[cursor];
        if (!candidate) continue;
        const at = candidate.kind === "message"
          ? candidate.entry.occurred_at
          : candidate.items.at(-1)?.finished_at ?? candidate.items.at(-1)?.started_at;
        if (at && Date.parse(at) > Date.parse(lastAt)) lastAt = at;
      }
      const firstAssistant = timeline.findIndex(
        (candidate, candidateIndex) =>
          candidateIndex > index &&
          candidate?.kind === "message" &&
          candidate.entry.role === "assistant",
      );
      if (firstAssistant !== -1 && firstAssistant <= endIndex) {
        const elapsed = durationLabel(startAt, lastAt);
        if (elapsed) result.set(firstAssistant, elapsed);
      }
    }
    return result;
  }, [timeline]);

  const liveAssistant = useMemo(() => {
    if (!assistantText || !run) return false;
    if (!["running", "waiting_tool", "pausing", "paused", "cancelling"].includes(run.status)) return false;
    return !conversation.some(
      (entry) =>
        entry.role === "assistant" &&
        entry.run_id === run.id &&
        assistantStartedAt &&
        Date.parse(entry.occurred_at) >= Date.parse(assistantStartedAt),
    );
  }, [assistantStartedAt, assistantText, conversation, run]);

  useEffect(() => {
    stickRef.current = true;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [sessionId]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !stickRef.current) return;
    const jump = () => {
      element.scrollTop = element.scrollHeight;
    };
    jump();
    const timer = window.setTimeout(jump, 160);
    return () => window.clearTimeout(timer);
  }, [assistantText, conversation.length, sessionId, visibleTools.length]);

  const runningTools = visibleTools.filter((item) =>
    ["preparing", "accepted", "received", "starting", "running"].includes(item.status),
  ).length;

  const currency = usage ? Object.entries(usage.by_currency)[0] : null;
  const cost = currency ? `${currency[1].known_cost} ${currency[0]}` : null;
  const runLine = (() => {
    if (!run) return { tone: "idle", text: "任务尚未开始运行" };
    switch (run.status) {
      case "queued":
        return { tone: "idle", text: "排队中 · 等待运行器接手" };
      case "running":
        return { tone: "", text: modelPhase ? `${modelPhase} · 可继续补充要求` : "正在执行 · 可继续补充要求" };
      case "waiting_tool":
        return { tone: "warn", text: `等待工具结果${runningTools ? ` · ${runningTools} 项未完成` : ""}` };
      case "waiting_user":
        return { tone: "warn", text: "等待你的输入 · 未派发新操作" };
      case "paused":
        return { tone: "idle", text: "已暂停 · 发送新指令继续" };
      case "recovering":
        return { tone: "warn", text: "运行已中断 · 恢复前需核对远端" };
      case "completed":
        return { tone: "done", text: "本阶段已结束" };
      case "failed":
        return { tone: "fail", text: "运行失败 · 查看任务详情" };
      case "cancelled":
        return { tone: "idle", text: "本轮已取消" };
      case "pausing":
        return { tone: "idle", text: "正在暂停 · 等待执行端确认" };
      case "cancelling":
        return { tone: "idle", text: "正在取消 · 等待执行状态确认" };
      default:
        return { tone: "idle", text: runLabel(run.status) };
    }
  })();

  return (
    <section className="view chat-view" aria-label="对话"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        if (event.defaultPrevented) return;
        event.preventDefault();
        if (busy) return;
        const input = event.currentTarget.querySelector<HTMLInputElement>('input[type="file"]');
        if (input) {
          input.files = event.dataTransfer.files;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }}>
      <div
        className="chat-scroll"
        ref={scrollRef}
        onScroll={(event) => {
          const element = event.currentTarget;
          stickRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
        }}
      >
        <div className="chat-inner">
          {hasMoreMessages && (
            <button className="load-more" disabled={loadingMore} onClick={onLoadOlderMessages}>
              {loadingMore ? "读取中…" : "查看更早的消息"}
            </button>
          )}
          {hasMoreTools && (
            <button className="load-more" disabled={loadingMore} onClick={onLoadMoreTools}>
              {loadingMore ? "读取中…" : "查看更早的工具操作"}
            </button>
          )}

          {timeline.map((item, index) => {
            if (item.kind === "message") {
              return (
                <div key={`${item.entry.source}:${item.entry.id}`}>
                  {item.entry.role === "assistant" && turnMeta.get(index) && (
                    <div className="turn-meta">
                      用时 {turnMeta.get(index)}
                      <Icon name="chev-r" size="sm" />
                    </div>
                  )}
                  <MessageBody sessionId={sessionId} entry={item.entry} />
                </div>
              );
            }
            return (
              <ActivityGroup
                key={`${item.items[0]?.run_id}:${item.items[0]?.tool_call_id}`}
                sessionId={sessionId}
                items={item.items}
                onOpenTool={onOpenTool}
              />
            );
          })}

          {liveAssistant && (
            <div className="msg">
              <div className="body">
                <div style={{ whiteSpace: "pre-wrap" }}>{assistantText}</div>
                <div className="msg-meta-line"><span>正在接收模型输出…</span></div>
              </div>
            </div>
          )}

          {run && (
            <StateCard
              run={run}
              inputRequired={inputRequired}
              tools={visibleTools}
              onControl={onControl}
              onAck={onAck}
              onOpenDrawer={onOpenDrawer}
              onCreateReport={onCreateReport}
              onCreateReportBusy={onCreateReportBusy}
            />
          )}

          <WeaknessDigest weaknesses={weaknesses} onOpen={onOpenWeakness} onEvidence={onOpenEvidence} />

          {!timeline.length && !liveAssistant && (
            <div className="drawer-empty">
              这个任务还没有可读的对话记录。运行器接手后，消息、工具操作与状态会实时出现在这里。
            </div>
          )}
        </div>
      </div>

      <div className="chat-foot">
        <div className="chat-foot-inner">
          <div className={`run-line ${runLine.tone}`}>
            <span className="pulse" />
            <span>{runLine.text}</span>
            {usage && (
              <span className="mono">
                · {usage.call_count} 次模型请求{cost ? ` · ${cost}` : ""}
                {usage.unpriced_calls > 0 ? ` · ${usage.unpriced_calls} 次未定价` : ""}
              </span>
            )}
          </div>
          <Composer
            attachmentsEnabled={attachmentsEnabled}
            sessionId={sessionId}
            onPrepareAttachments={onPrepareAttachments}
            models={models}
            modelKey={modelKey}
            modelLocked={modelLocked}
            onModelChange={onModelChange}
            onConfigureModel={onConfigureModel}
            placeholder={
              run?.status === "waiting_user"
                ? "补充指令，或点击继续运行"
                : canStop(run?.status)
                  ? "补充指令，或点击右侧暂停本轮"
                  : run?.status === "paused"
                    ? "发送新指令，继续本轮"
                    : run && ["completed", "cancelled", "failed"].includes(run.status)
                      ? "发送新指令，开始下一阶段"
                      : run
                        ? "补充指令，或等待当前运行结束"
                        : "询问 Rexak"
            }
            busy={busy}
            scopeMode={scopeMode}
            onScope={() => onOpenDrawer("settings")}
            onSend={onSend}
            onStop={() => onControl("pause")}
            stopper={canStop(run?.status)}
            onToast={onToast}
          />
        </div>
      </div>
    </section>
  );
}
