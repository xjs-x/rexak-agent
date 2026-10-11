import { useEffect, useRef, useState } from "react";
import {
  api,
  ApiRequestError,
  type AgentEvent,
  type ArtifactPreview,
  type ConversationEntry,
  type FindingItem,
  type JobItem,
  type LeadItem,
  type ModelCallItem,
  type ModelSettings,
  type OperationItem,
  type ToolActivityItem,
  type PlanRead,
  type PriceCatalog,
  type ProjectItem,
  type ReportItem,
  type RunItem,
  type SessionItem,
  type SpanItem,
  type Snapshot,
  type UsageSummary,
} from "./api/client";
import { PlanGraph } from "./PlanGraph";
import { ToolActivityGroup } from "./ToolActivity";

type Tab = "activity" | "conversation" | "plan" | "findings" | "jobs" | "cost" | "diagnostics" | "reports" | "settings";
type Connection = "准备中" | "已连接" | "连接中断" | "正在同步";
const EVENT_ROW_HEIGHT = 82;
const EVENT_VIEWPORT_HEIGHT = 450;

const eventTypes = [
  "run.status",
  "message.delta",
  "message.completed",
  "plan.updated",
  "tool.accepted",
  "tool.output",
  "tool.completed",
  "lead.updated",
  "finding.updated",
  "coverage.updated",
  "usage.updated",
  "memory.compacted",
  "input.required",
  "report.ready",
  "error",
  "stream.resync_required",
];

const statusLabels: Record<string, string> = {
  queued: "排队中",
  running: "正在测试",
  waiting_tool: "等待工具结果",
  waiting_user: "需要你的输入",
  pausing: "正在暂停",
  paused: "已暂停",
  cancelling: "正在取消",
  cancelled: "已取消",
  completed: "本阶段已结束",
  failed: "运行失败",
  recovering: "等待恢复",
  stopped: "已停止",
  candidate: "待验证",
  confirmed: "已确认",
  disproved: "复测未支持",
  inconclusive: "证据不足",
  blocked: "受阻",
  deferred: "暂缓",
  input_tokens: "输入 tokens",
  output_tokens: "输出 tokens",
  cache_read_tokens: "缓存读取 tokens",
  cache_write_tokens: "缓存写入 tokens",
  reasoning_tokens: "推理 tokens",
  "run.status": "运行状态",
  "message.delta": "助手输出",
  "message.completed": "消息已接收",
  "plan.updated": "计划更新",
  "tool.accepted": "工具已受理",
  "tool.output": "工具输出",
  "tool.completed": "工具已结束",
  "lead.updated": "线索更新",
  "finding.updated": "发现更新",
  "coverage.updated": "覆盖更新",
  "usage.updated": "费用更新",
  "memory.compacted": "上下文整理",
  "input.required": "需要输入",
  "report.ready": "报告已生成",
  error: "错误",
  in_sync: "已同步",
  stale: "同步滞后",
  accepted: "已受理",
  succeeded: "已完成",
  rejected: "已拒绝",
  unknown: "状态未知",
};

function label(value: string | null | undefined): string {
  return value ? statusLabels[value] || value.replaceAll("_", " ") : "未知";
}

const spanTitles: Record<string, string> = {
  "finish_gate.middleware": "提议检查",
  "finish_gate.runner": "片段结束复核",
  "report.generate": "报告生成总耗时",
  "report.capture": "报告快照采集",
  "report.verify": "报告证据校验",
  "report.render": "报告正文渲染",
  "report.publish": "报告版本发布",
  "verification.git_head.preflight": "Git HEAD 复核预检查",
  "verification.git_head.recheck_dispatch": "Git HEAD 复测调用返回",
  "verification.git_head.confirm": "Git HEAD 结论写入",
};

function time(value: string | null | undefined): string {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

function duration(start: string | null | undefined, end: string | null | undefined): string {
  if (!start || !end) return "未知（时间戳不完整）";
  const elapsed = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(elapsed) || elapsed < 0) return "时间戳不可比较";
  return elapsed < 1000 ? `${elapsed} 毫秒` : `${(elapsed / 1000).toFixed(2)} 秒`;
}

function text(value: unknown, max = 280): string {
  const raw = typeof value === "string" ? value : JSON.stringify(value) ?? "";
  return raw.length > max ? `${raw.slice(0, max)}…` : raw;
}

function eventSummary(event: AgentEvent): string {
  const payload = event.payload as Record<string, unknown>;
  if (event.type === "message.delta") return text(payload.delta ?? "", 160);
  if (event.type === "run.status" && payload.scope === "model_request") return modelPhaseLabels[String(payload.phase)] ?? "模型请求状态更新";
  if (event.type === "run.status") return `运行状态：${label(String(payload.status ?? ""))}`;
  if (event.type === "input.required") return "测试需要你补充条件";
  if (event.type === "report.ready") return "报告版本已生成";
  return text(payload, 260);
}

const modelPhaseLabels: Record<string, string> = {
  waiting: "等待模型响应",
  thinking: "模型正在思考",
  responding: "模型正在生成回复或工具调用",
  finished: "模型请求已结束",
};

function latestRun(snapshot: Snapshot | null): RunItem | null {
  return snapshot?.runs.at(-1) ?? null;
}

function mergeLatestEvents(...groups: AgentEvent[][]): AgentEvent[] {
  const bySequence = new Map<number, AgentEvent>();
  for (const event of groups.flat()) bySequence.set(event.session_seq, event);
  return [...bySequence.values()].sort((left, right) => left.session_seq - right.session_seq).slice(-200);
}

function mergeToolActivities(...groups: ToolActivityItem[][]): ToolActivityItem[] {
  const items = new Map<string, ToolActivityItem>();
  for (const item of groups.flat()) items.set(`${item.run_id}:${item.tool_call_id}`, item);
  return [...items.values()].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at));
}

type ChatTimelineItem =
  | { kind: "message"; entry: ConversationEntry }
  | { kind: "tools"; entries: [ToolActivityItem, ...ToolActivityItem[]] };

function chatTimeline(messages: ConversationEntry[], tools: ToolActivityItem[]): ChatTimelineItem[] {
  const ordered = [
    ...messages.map((entry) => ({ kind: "message" as const, entry, at: entry.occurred_at })),
    ...tools.map((entry) => ({ kind: "tool" as const, entry, at: entry.started_at })),
  ].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const timeline: ChatTimelineItem[] = [];
  for (const item of ordered) {
    if (item.kind === "message") {
      timeline.push({ kind: "message", entry: item.entry });
    } else {
      const previous = timeline.at(-1);
      // 仅合并同一运行中连续的操作，保留回复与工具的时间顺序。
      if (previous?.kind === "tools" && previous.entries[0].run_id === item.entry.run_id) previous.entries.push(item.entry);
      else timeline.push({ kind: "tools", entries: [item.entry] });
    }
  }
  return timeline;
}

function VirtualEventList({ events, onSelect }: {
  events: AgentEvent[];
  onSelect: (event: AgentEvent) => void;
}) {
  const [scrollTop, setScrollTop] = useState(0);
  const first = Math.max(0, Math.floor(scrollTop / EVENT_ROW_HEIGHT) - 4);
  const last = Math.min(events.length, first + Math.ceil(EVENT_VIEWPORT_HEIGHT / EVENT_ROW_HEIGHT) + 8);
  if (events.length === 0) return <div className="event-list"><p className="empty">暂无活动记录</p></div>;
  return <div className="event-list" onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
    <div className="event-spacer" style={{ height: events.length * EVENT_ROW_HEIGHT }}>
      {events.slice(first, last).map((event, index) => <button
        type="button"
        className="event-row"
        style={{ top: (first + index) * EVENT_ROW_HEIGHT }}
        key={event.event_id}
        onClick={() => onSelect(event)}
        aria-label={`查看事件 #${event.session_seq}`}
      >
        <span className="event-number">#{event.session_seq}</span>
        <span className="event-content"><strong>{label(event.type)}</strong><small>{time(event.occurred_at)}</small><span className="event-summary">{eventSummary(event)}</span></span>
      </button>)}
    </div>
  </div>;
}

function ChatMessage({ sessionId, entry }: { sessionId: string; entry: ConversationEntry }) {
  const [body, setBody] = useState(entry.preview);
  const [error, setError] = useState("");
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const identity = useRef("");
  useEffect(() => {
    let active = true;
    identity.current = `${sessionId}:${entry.id}`;
    setBody(entry.preview);
    setError("");
    setNextOffset(null);
    setLoading(false);
    if (entry.available && entry.has_more) void api.conversationBody(sessionId, entry).then(
      (page) => { if (active) { setBody(page.text); setNextOffset(page.next_offset); } },
      (cause) => active && setError(String(cause)),
    );
    return () => { active = false; };
  }, [sessionId, entry.id, entry.preview, entry.has_more, entry.available]);
  const loadMore = async () => {
    if (nextOffset === null || loading) return;
    const requestIdentity = identity.current;
    setLoading(true);
    try {
      const page = await api.conversationBody(sessionId, entry, nextOffset);
      if (identity.current === requestIdentity) {
        setBody((previous) => previous + page.text);
        setNextOffset(page.next_offset);
        setError("");
      }
    } catch (cause) {
      if (identity.current === requestIdentity) setError(String(cause));
    } finally {
      if (identity.current === requestIdentity) setLoading(false);
    }
  };
  if (!body && !error) return null;
  return <article className={`chat-message ${entry.role === "user" ? "user" : "assistant"}`} data-role={entry.role}>
    <div className="message-body">{entry.role === "user" ? <p>{body}</p> : <div className="markdown-text">{body}</div>}</div>
    {error && <small className="body-error">正文加载失败：{error}</small>}
    {nextOffset !== null && <button disabled={loading} onClick={() => void loadMore()}>{loading ? "加载中…" : "展开剩余正文"}</button>}
  </article>;
}

function useProjectData() {
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [projectId, setProjectId] = useState("");
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let connecting = false;
    let bootstrapped = false;
    let timer: ReturnType<typeof setTimeout>;
    const disconnected = () => {
      bootstrapped = false;
      setReady(false);
      setError("本机服务已断开。请启动 Rexak Agent；恢复后页面会自动重连，输入已保留。");
    };
    window.addEventListener("workbench:connection-lost", disconnected);
    const connect = async () => {
      if (!active || connecting) return;
      connecting = true;
      try {
        await api.health();
        if (bootstrapped) return;
        await api.signIn();
        const data = await api.projects();
        if (!active) return;
        setProjects(data.projects);
        const preferred = new URLSearchParams(location.search).get("session");
        let preferredProjectId: string | undefined;
        if (preferred) {
          try {
            const snap = await api.snapshot(preferred);
            preferredProjectId = snap.session.project_id;
          } catch {
            // 已删除或不可读的旧会话交给项目列表兜底。
          }
        }
        if (!active) return;
        setProjectId(
          data.projects.find((item) => item.id === preferredProjectId)?.id ??
            data.projects.find((item) => item.name === "我的工作台")?.id ?? "",
        );
        bootstrapped = true;
        setError("");
        setReady(true);
      } catch {
        if (active) disconnected();
      } finally {
        connecting = false;
        if (active) timer = setTimeout(() => void connect(), 5000);
      }
    };
    void connect();
    return () => {
      active = false;
      clearTimeout(timer);
      window.removeEventListener("workbench:connection-lost", disconnected);
    };
  }, []);

  useEffect(() => {
    if (!projectId || !ready) { setSessions([]); return; }
    let active = true;
    void api.sessions(projectId).then(
      (data) => {
        if (!active) return;
        setSessions(data.sessions);
        const preferred = new URLSearchParams(location.search).get("session");
        setSessionId((current) =>
          data.sessions.some((item) => item.id === current)
            ? current
            : data.sessions.find((item) => item.id === preferred)?.id ?? "",
        );
      },
      (cause) => active && setError(String(cause)),
    );
    return () => {
      active = false;
    };
  }, [projectId, ready]);

  const refreshProjects = async () => {
    const data = await api.projects();
    setProjects(data.projects);
  };
  const refreshSessions = async (id: string) => {
    const data = await api.sessions(id);
    setSessions(data.sessions);
  };
  return {
    projects, projectId, setProjectId, sessions, sessionId, setSessionId,
    ready, error, setError, refreshProjects, refreshSessions,
  };
}

export default function App() {
  const project = useProjectData();
  const [tab, setTab] = useState<Tab>("conversation");
  const [showDetails, setShowDetails] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 760);
  const [deleteTarget, setDeleteTarget] = useState<SessionItem | null>(null);
  const [showRecoveryConfirm, setShowRecoveryConfirm] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const followChatRef = useRef(true);
  const toolHistoryRequestedRef = useRef(false);
  const conversationCursorRef = useRef<string | null>(null);
  const detailsVisibleRef = useRef(false);
  const detailTabRef = useRef<Tab>("conversation");
  const refreshDetailsRef = useRef<(() => Promise<void>) | null>(null);
  useEffect(() => {
    detailsVisibleRef.current = showDetails;
    detailTabRef.current = tab;
    void refreshDetailsRef.current?.();
  }, [showDetails, tab]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [olderBefore, setOlderBefore] = useState<number | null>(null);
  const [historyBefore, setHistoryBefore] = useState<number | null>(null);
  const [historyBackStack, setHistoryBackStack] = useState<Array<number | null>>([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const historyBusyRef = useRef(false);
  const activeSessionRef = useRef("");
  const liveTailRef = useRef<AgentEvent[]>([]);
  const [historyMode, setHistoryMode] = useState(false);
  const historyModeRef = useRef(false);
  const [assistantText, setAssistantText] = useState("");
  const assistantResponseRef = useRef("");
  const [assistantStartedAt, setAssistantStartedAt] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>("准备中");
  const [findings, setFindings] = useState<FindingItem[]>([]);
  const [leads, setLeads] = useState<LeadItem[]>([]);
  const [plan, setPlan] = useState<PlanRead | null>(null);
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [modelCalls, setModelCalls] = useState<ModelCallItem[]>([]);
  const [operations, setOperations] = useState<OperationItem[]>([]);
  const [toolActivities, setToolActivities] = useState<ToolActivityItem[]>([]);
  const [toolNext, setToolNext] = useState<number | null>(null);
  const [toolBusy, setToolBusy] = useState(false);
  const [toolError, setToolError] = useState("");
  const [spans, setSpans] = useState<SpanItem[]>([]);
  const [reports, setReports] = useState<ReportItem[]>([]);
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [priceCatalog, setPriceCatalog] = useState<PriceCatalog | null>(null);
  const [priceProfileIndex, setPriceProfileIndex] = useState(0);
  const [priceForm, setPriceForm] = useState({
    currency: "CNY", input: "", output: "", cacheRead: "", cacheWrite: "",
    cacheIncluded: false, reasoningIncluded: false,
  });
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [eventDetail, setEventDetail] = useState<AgentEvent | null>(null);
  const [conversation, setConversation] = useState<ConversationEntry[]>([]);
  const [conversationNext, setConversationNext] = useState<string | null>(null);
  const [conversationCursor, setConversationCursor] = useState<string | null>(null);
  const [conversationBackStack, setConversationBackStack] = useState<Array<string | null>>([]);
  const [conversationBusy, setConversationBusy] = useState(false);
  const [conversationDetail, setConversationDetail] = useState<{
    entry: ConversationEntry; text: string; nextOffset: number | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [message, setMessage] = useState("");
  const [showProjectForm, setShowProjectForm] = useState(false);
  const [showSessionForm, setShowSessionForm] = useState(false);
  const [runMode, setRunMode] = useState<"instruction" | "scoped">("instruction");
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [sessionTitle, setSessionTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [targetUrl, setTargetUrl] = useState("");

  const currentRun = latestRun(snapshot);
  const modelProgress = events.filter((event) => event.run_id === currentRun?.id && event.type === "run.status" && event.payload?.scope === "model_request").at(-1);
  const modelProgressLabel = modelProgress && modelProgress.payload?.phase !== "finished"
    ? modelPhaseLabels[String(modelProgress.payload?.phase)] : null;
  useEffect(() => { conversationCursorRef.current = conversationCursor; }, [conversationCursor]);
  useEffect(() => {
    if (followChatRef.current) chatEndRef.current?.scrollIntoView({ block: "end" });
  }, [conversation.length, toolActivities.length, assistantText, project.sessionId]);
  const selectedPriceProfile = modelSettings?.profiles[priceProfileIndex];
  const diagnosticRows = [
    ...modelCalls.map((item) => ({ kind: "model" as const, at: item.submitted_at, item })),
    ...operations.map((item) => ({ kind: "tool" as const, at: item.created_at, item })),
    ...spans.map((item) => ({ kind: "verification" as const, at: item.started_at, item })),
  ].sort((left, right) => Date.parse(right.at ?? "") - Date.parse(left.at ?? ""));

  const nextModelGap = (operation: OperationItem): string | null => {
    if (["accepted", "running"].includes(operation.status)) return null;
    const ended = Date.parse(operation.updated_at);
    const next = modelCalls
      .filter((call) => call.run_id === operation.run_id && call.submitted_at && Date.parse(call.submitted_at) >= ended)
      .sort((left, right) => Date.parse(left.submitted_at ?? "") - Date.parse(right.submitted_at ?? ""))[0];
    return next ? duration(operation.updated_at, next.submitted_at) : null;
  };

  useEffect(() => {
    if (tab !== "settings" || !project.ready) return;
    void api.priceCatalog().then(setPriceCatalog).catch((cause) => setNotice(String(cause)));
  }, [tab, project.ready]);

  useEffect(() => {
    const price = priceCatalog?.prices.find((item) =>
      item.provider_id === selectedPriceProfile?.provider_id && item.model_name === selectedPriceProfile?.model_name,
    );
    setPriceForm({
      currency: price?.currency ?? "CNY",
      input: price?.input_per_unit ?? "",
      output: price?.output_per_unit ?? "",
      cacheRead: price?.cache_read_per_unit ?? "",
      cacheWrite: price?.cache_write_per_unit ?? "",
      cacheIncluded: price?.cache_tokens_included_in_input ?? false,
      reasoningIncluded: price?.reasoning_tokens_included_in_output ?? false,
    });
  }, [priceCatalog?.version, selectedPriceProfile?.provider_id, selectedPriceProfile?.model_name]);

  useEffect(() => {
    if (!project.sessionId || !project.ready) {
      activeSessionRef.current = "";
      setSnapshot(null);
      setPlan(null);
      setConversation([]);
      setAssistantText("");
      return;
    }
    let closed = false;
    let source: EventSource | null = null;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    let watchdogTimer: ReturnType<typeof setInterval> | null = null;
    let recoveryPending = false;
    let detailsRefreshing = false;
    let lastEventAt = Date.now();
    let lastSeq = 0;
    const sessionId = project.sessionId;
    activeSessionRef.current = sessionId;
    setSnapshot(null);
    setUsage(null);
    setFindings([]);
    setLeads([]);
    setJobs([]);
    setModelCalls([]);
    setOperations([]);
    followChatRef.current = true;
    setToolActivities([]);
    setToolNext(null);
    toolHistoryRequestedRef.current = false;
    setToolError("");
    setSpans([]);
    setReports([]);
    historyBusyRef.current = false;
    liveTailRef.current = [];
    setHistoryBusy(false);
    setEvents([]);
    setPlan(null);
    setOlderBefore(null);
    setHistoryBefore(null);
    setHistoryBackStack([]);
    setAssistantText("");
    setEventDetail(null);
    setConversation([]);
    setConversationNext(null);
    setConversationCursor(null);
    setConversationBackStack([]);
    setConversationDetail(null);
    historyModeRef.current = false;
    setHistoryMode(false);
    setConnection("正在同步");
    history.replaceState(null, "", `?session=${encodeURIComponent(sessionId)}`);

    const refreshDetails = async () => {
      if (detailsRefreshing || closed) return;
      detailsRefreshing = true;
      const visible = detailsVisibleRef.current;
      const currentTab = detailTabRef.current;
      const results = await Promise.allSettled([
        api.snapshot(sessionId), visible && currentTab === "findings" ? api.findings(sessionId) : Promise.resolve(null),
        visible && ["findings", "plan"].includes(currentTab) ? api.leads(sessionId) : Promise.resolve(null),
        visible && currentTab === "jobs" ? api.jobs(sessionId) : Promise.resolve(null), api.usage(sessionId),
        visible && ["cost", "diagnostics"].includes(currentTab) ? api.modelCalls(sessionId) : Promise.resolve(null),
        visible && currentTab === "diagnostics" ? api.operations(sessionId) : Promise.resolve(null),
        visible && currentTab === "diagnostics" ? api.spans(sessionId) : Promise.resolve(null),
        visible && currentTab === "reports" ? api.reports(sessionId) : Promise.resolve(null),
        visible && currentTab === "settings" ? api.modelSettings() : Promise.resolve(null),
        visible && currentTab === "plan" ? api.plan(sessionId) : Promise.resolve(null), api.conversation(sessionId),
        api.toolActivities(sessionId),
      ]);
      detailsRefreshing = false;
      if (closed) return;
      const [snap, found, lead, job, cost, calls, operation, span, report, settings, currentPlan, chat, activity] = results;
      if (snap.status === "fulfilled" && snap.value.event_cursor >= lastSeq) {
        setSnapshot(snap.value);
      }
      if (found.status === "fulfilled" && found.value) setFindings(found.value.findings);
      if (lead.status === "fulfilled" && lead.value) setLeads(lead.value.leads);
      if (job.status === "fulfilled" && job.value) setJobs(job.value.jobs);
      if (cost.status === "fulfilled") setUsage(cost.value);
      if (calls.status === "fulfilled" && calls.value) setModelCalls(calls.value.model_calls);
      if (operation.status === "fulfilled" && operation.value) setOperations(operation.value.operations);
      if (span.status === "fulfilled" && span.value) setSpans(span.value.spans);
      if (report.status === "fulfilled" && report.value) setReports(report.value.reports);
      if (settings.status === "fulfilled" && settings.value) setModelSettings(settings.value);
      if (currentPlan.status === "fulfilled" && currentPlan.value) setPlan(currentPlan.value);
      if (activity.status === "fulfilled") {
        setToolActivities((previous) => mergeToolActivities(previous, activity.value.activities));
        if (!toolHistoryRequestedRef.current) setToolNext(activity.value.next_offset);
        setToolError("");
      } else { setToolError("工具进度暂时无法同步，正在重试。"); }
      if (chat.status === "fulfilled" && !conversationCursorRef.current) {
        setConversation(chat.value.entries);
        setConversationNext(chat.value.next_cursor);
      }
    };
    refreshDetailsRef.current = refreshDetails;

    const scheduleRefresh = () => {
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => void refreshDetails(), 400);
    };

    const start = async () => {
      source?.close();
      liveTailRef.current = [];
      historyModeRef.current = false;
      setHistoryMode(false);
      setHistoryBefore(null);
      setHistoryBackStack([]);
      setConnection("正在同步");
      try {
        const [snap, historyPage, conversationPage] = await Promise.all([
          api.snapshot(sessionId), api.eventPage(sessionId), api.conversation(sessionId),
        ]);
        if (closed) return;
        // 已显示事件与快照游标可能在并发读取时错位；流只从已显示事件之后续读。
        lastSeq = historyPage.events.at(-1)?.session_seq ?? 0;
        setSnapshot((previous) =>
          previous && previous.session.id === snap.session.id && previous.event_cursor > snap.event_cursor
            ? previous
            : snap,
        );
        setEvents(historyPage.events);
        setConversation(conversationPage.entries);
        setConversationNext(conversationPage.next_cursor);
        setConversationCursor(null);
        setConversationBackStack([]);
        setOlderBefore(historyPage.next_before);
        const historyDeltas = historyPage.events
            .filter((event) => event.type === "message.delta")
            .filter((event) => event.run_id === latestRun(snap)?.id && latestRun(snap)?.status === "running");
        const responseId = String((historyDeltas.at(-1)?.payload as Record<string, unknown> | undefined)?.response_id ?? "");
        const latestDeltas = responseId ? historyDeltas.filter((event) => (event.payload as Record<string, unknown>).response_id === responseId) : historyDeltas;
        assistantResponseRef.current = responseId;
        setAssistantStartedAt(latestDeltas[0]?.occurred_at ?? null);
        setAssistantText(latestDeltas
            .map((event) => String((event.payload as Record<string, unknown>).delta ?? ""))
            .join("")
            .slice(-20000),
        );
        await refreshDetails();
        if (closed) return;
        lastEventAt = Date.now();
        source = new EventSource(`/api/sessions/${sessionId}/events?after=${lastSeq}`);
        source.onopen = () => !closed && setConnection("已连接");
        source.onerror = () => !closed && setConnection("连接中断");
        for (const type of eventTypes) {
          source.addEventListener(type, (raw) => {
            if (closed) return;
            let event: AgentEvent;
            try {
              event = JSON.parse((raw as MessageEvent).data) as AgentEvent;
            } catch {
              return;
            }
            if (event.type === "stream.resync_required") {
              recover();
              return;
            }
            if (event.session_seq <= lastSeq) return;
            lastSeq = event.session_seq;
            lastEventAt = Date.now();
            if (historyModeRef.current) {
              liveTailRef.current = mergeLatestEvents(liveTailRef.current, [event]);
            } else {
              setEvents((previous) => mergeLatestEvents(previous, [event]));
            }
            if (event.type === "message.delta") {
              const payload = event.payload as Record<string, unknown>;
              const delta = String(payload.delta ?? "");
              const id = String(payload.response_id ?? event.run_id ?? "");
              if (id !== assistantResponseRef.current) {
                assistantResponseRef.current = id;
                setAssistantStartedAt(event.occurred_at);
                setAssistantText(delta.slice(-20000));
              } else {
                setAssistantText((previous) => (previous + delta).slice(-20000));
              }
            }
            if (event.type !== "message.delta") scheduleRefresh();
          });
        }
      } catch (cause) {
        if (!closed) {
          setConnection("连接中断");
          setNotice(`无法读取任务：${String(cause)}`);
        }
      }
    };
    const recover = () => {
      if (closed || recoveryPending) return;
      if (detailsRefreshing) return;
      recoveryPending = true;
      void start().finally(() => { recoveryPending = false; });
    };
    recover();
    watchdogTimer = setInterval(() => {
      if (closed || recoveryPending) return;
      void api.snapshot(sessionId).then((snap) => {
        if (closed || recoveryPending) return;
        void refreshDetails();
        if (!source || source.readyState === EventSource.CLOSED ||
            (snap.event_cursor > lastSeq && Date.now() - lastEventAt > 3000)) recover();
      }).catch(() => { if (!closed) setConnection("连接中断"); });
    }, 3000);
    return () => {
      closed = true;
      source?.close();
      if (refreshDetailsRef.current === refreshDetails) refreshDetailsRef.current = null;
      if (refreshTimer) clearTimeout(refreshTimer);
      if (watchdogTimer) clearInterval(watchdogTimer);
    };
  }, [project.sessionId, project.ready]);

  const runAction = async (
    action: () => Promise<unknown>, success: string, refreshCurrent = true,
  ) => {
    setBusy(true);
    setNotice("");
    try {
      await action();
      setNotice(success);
      if (refreshCurrent && project.sessionId) {
        const [snap, report, cost, diagnostics] = await Promise.all([
          api.snapshot(project.sessionId),
          api.reports(project.sessionId),
          api.usage(project.sessionId),
          api.spans(project.sessionId),
        ]);
        setSnapshot((previous) =>
          previous && previous.session.id === snap.session.id && previous.event_cursor > snap.event_cursor
            ? previous
            : snap,
        );
        setReports(report.reports);
        setUsage(cost);
        setSpans(diagnostics.spans);
      }
      return true;
    } catch (cause) {
      setNotice(String(cause));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const savePrice = async () => {
    if (!selectedPriceProfile || !priceCatalog) return;
    const rate = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
    if (!rate.test(priceForm.input) || !rate.test(priceForm.output) ||
      (priceForm.cacheRead !== "" && !rate.test(priceForm.cacheRead)) ||
      (priceForm.cacheWrite !== "" && !rate.test(priceForm.cacheWrite))) {
      setNotice("请填写非负的每百万 token 单价；缓存价格可留空");
      return;
    }
    if (!/^[A-Z]{3}$/.test(priceForm.currency)) {
      setNotice("货币代码须为三个大写字母，例如 CNY 或 USD");
      return;
    }
    await runAction(async () => {
      const updated = await api.updatePrice({
        expected_version: priceCatalog.version,
        provider_id: selectedPriceProfile.provider_id,
        model_name: selectedPriceProfile.model_name,
        currency: priceForm.currency,
        input_per_1m: priceForm.input,
        output_per_1m: priceForm.output,
        cache_read_per_1m: priceForm.cacheRead || null,
        cache_write_per_1m: priceForm.cacheWrite || null,
        cache_tokens_included_in_input: priceForm.cacheIncluded,
        reasoning_tokens_included_in_output: priceForm.reasoningIncluded,
      });
      setPriceCatalog(updated);
      setModelSettings((previous) => previous ? { ...previous, price_catalog_version: updated.version } : previous);
    }, "价格已保存；之后启动的运行使用新版本", false);
  };

  const submitProject = async () => {
    const name = projectName.trim();
    if (!name) return setNotice("请填写项目名称");
    await runAction(async () => {
      const created = await api.createProject({
        name,
        target_description: projectDescription.trim() || name,
        settings_version: "1",
      });
      await project.refreshProjects();
      project.setProjectId(created.id);
      setProjectName("");
      setProjectDescription("");
      setShowProjectForm(false);
    }, "项目已创建", false);
  };

  const ensureProject = async () => {
    if (project.projectId) return project.projectId;
    const existing = (await api.projects()).projects.find((item) => item.name === "我的工作台");
    const created = existing ?? await api.createProject({ name: "我的工作台", target_description: "个人任务与对话", settings_version: "1" });
    await project.refreshProjects();
    project.setProjectId(created.id);
    return created.id;
  };

  const createChat = async (content: string, title: string, url = "", mode: "instruction" | "scoped" = "instruction") => {
    let target: URL | null = null;
    if (url.trim()) {
      try { target = new URL(url.trim()); }
      catch { throw new Error("请输入完整的 http:// 或 https:// 目标地址"); }
      if (!["http:", "https:"].includes(target.protocol) || !target.hostname) {
        throw new Error("当前控制台只接受 HTTP 或 HTTPS 目标");
      }
    }
    const projectId = await ensureProject();
    const created = await api.createSession(projectId, {
      project_id: projectId, title, initial_objective: content,
      scope: { mode, revision: 1, rules: target ? [{
        scheme: mode === "instruction" ? "any" : target.protocol === "https:" ? "https" : "http",
        host: target.hostname.replace(/^\[|\]$/g, ""),
        port: mode === "instruction" ? null : target.port ? Number(target.port) : target.protocol === "https:" ? 443 : 80,
        include_subdomains: false, allow_private: true,
      }] : [] },
    });
    project.setSessionId(created.session.id);
    setTab("conversation");
    try { await project.refreshSessions(projectId); }
    catch { setNotice("任务已创建；列表暂时无法刷新，连接恢复后会重新加载。请勿重复提交。"); }
  };

  const submitSession = async () => {
    const title = sessionTitle.trim();
    const initialObjective = objective.trim();
    if (!title || !initialObjective) return setNotice("请填写任务名称和测试目标");
    await runAction(async () => {
      await createChat(initialObjective, title, targetUrl, runMode);
      setSessionTitle("");
      setObjective("");
      setTargetUrl("");
      setShowSessionForm(false);
    }, "测试任务已创建，正在等待运行器接手", false);
  };

  const sendMessage = async () => {
    if (busy || !message.trim() || !project.ready) return;
    const content = message.trim();
    const accepted = await runAction(async () => {
      if (project.sessionId) await api.message(project.sessionId, content);
      else await createChat(content, content.slice(0, 30));
    }, "", false);
    if (accepted) {
      setMessage("");
      setAssistantText("");
      assistantResponseRef.current = "";
      setAssistantStartedAt(null);
      if (project.sessionId) await refreshConversation();
    }
  };

  const resumeCurrentRun = async () => {
    if (!currentRun) return;
    await runAction(async () => {
      try { await api.control(currentRun.id, "resume"); }
      catch (cause) {
        if (cause instanceof ApiRequestError && cause.status === 409 && cause.details.required_field === "acknowledge_unknown_operations") setShowRecoveryConfirm(true);
        throw cause;
      }
    }, "恢复指令已提交");
  };

  const newChat = () => {
    setRunMode("instruction");
    project.setSessionId(""); setTab("conversation"); setShowDetails(false);
    setMessage(""); setNotice(""); setShowRecoveryConfirm(false);
    history.replaceState(null, "", location.pathname);
  };

  const removeTask = async () => {
    if (!deleteTarget) return;
    let pending = false;
    const removed = await runAction(async () => {
      const result = await api.deleteSession(deleteTarget.id);
      pending = result.cancellation_pending;
      if (project.sessionId === deleteTarget.id) newChat();
      await project.refreshSessions(deleteTarget.project_id);
      setDeleteTarget(null);
    }, "", false);
    if (removed && pending) setNotice("任务已从列表移除；后台仍在确认取消结果。");
  };

  const showEvidence = async (id: string) => {
    try {
      setPreview(await api.artifact(id));
    } catch (cause) {
      setNotice(String(cause));
    }
  };

  const loadOlder = async () => {
    if (!project.sessionId || olderBefore === null || historyBusyRef.current) return;
    const sessionId = project.sessionId;
    const nextBefore = olderBefore;
    historyBusyRef.current = true;
    setHistoryBusy(true);
    try {
      const page = await api.eventPage(sessionId, nextBefore);
      if (activeSessionRef.current !== sessionId) return;
      historyModeRef.current = true;
      setHistoryMode(true);
      setHistoryBackStack((previous) => [...previous, historyBefore]);
      setHistoryBefore(nextBefore);
      setEvents(page.events);
      setOlderBefore(page.next_before);
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      historyBusyRef.current = false;
      setHistoryBusy(false);
    }
  };

  const backToLatest = async () => {
    if (!project.sessionId || historyBusyRef.current) return;
    const sessionId = project.sessionId;
    historyBusyRef.current = true;
    setHistoryBusy(true);
    try {
      const page = await api.eventPage(sessionId);
      if (activeSessionRef.current !== sessionId) return;
      historyModeRef.current = false;
      setHistoryMode(false);
      setHistoryBackStack([]);
      setHistoryBefore(null);
      setEvents(mergeLatestEvents(page.events, liveTailRef.current));
      liveTailRef.current = [];
      setOlderBefore(page.next_before);
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      historyBusyRef.current = false;
      setHistoryBusy(false);
    }
  };

  const loadNewer = async () => {
    if (!project.sessionId || !historyBackStack.length || historyBusyRef.current) return;
    const previousBefore = historyBackStack.at(-1) ?? null;
    if (previousBefore === null) {
      await backToLatest();
      return;
    }
    const sessionId = project.sessionId;
    historyBusyRef.current = true;
    setHistoryBusy(true);
    try {
      const page = await api.eventPage(sessionId, previousBefore);
      if (activeSessionRef.current !== sessionId) return;
      setHistoryBackStack((previous) => previous.slice(0, -1));
      setHistoryBefore(previousBefore);
      setEvents(page.events);
      setOlderBefore(page.next_before);
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      historyBusyRef.current = false;
      setHistoryBusy(false);
    }
  };

  const showEvent = async (event: AgentEvent) => {
    if (!project.sessionId) return;
    const sessionId = project.sessionId;
    try {
      const safeEvent = await api.eventAt(sessionId, event.session_seq);
      if (activeSessionRef.current === sessionId) setEventDetail(safeEvent);
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    }
  };

  const refreshConversation = async () => {
    if (!project.sessionId || conversationBusy) return;
    const sessionId = project.sessionId;
    setConversationBusy(true);
    try {
      const page = await api.conversation(sessionId);
      if (activeSessionRef.current === sessionId) {
        setConversation(page.entries);
        setConversationNext(page.next_cursor);
        setConversationCursor(null);
        setConversationBackStack([]);
      }
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      setConversationBusy(false);
    }
  };

  const loadEarlierTools = async () => {
    if (toolNext === null || toolBusy || !project.sessionId) return;
    const taskId = project.sessionId;
    toolHistoryRequestedRef.current = true;
    setToolBusy(true);
    try {
      const page = await api.toolActivities(taskId, toolNext);
      if (activeSessionRef.current !== taskId) return;
      setToolActivities((previous) => mergeToolActivities(previous, page.activities));
      setToolNext(page.next_offset);
    } catch (cause) { if (activeSessionRef.current === taskId) setToolError(String(cause)); }
    finally { setToolBusy(false); }
  };

  const loadMoreConversation = async () => {
    if (!project.sessionId || conversationNext === null || conversationBusy) return;
    const sessionId = project.sessionId;
    setConversationBusy(true);
    try {
      const page = await api.conversation(sessionId, conversationNext);
      if (activeSessionRef.current === sessionId) {
        setConversationBackStack((previous) => [...previous, conversationCursor]);
        setConversationCursor(conversationNext);
        setConversation(page.entries);
        setConversationNext(page.next_cursor);
      }
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      setConversationBusy(false);
    }
  };

  const loadNewerConversation = async () => {
    if (!project.sessionId || !conversationBackStack.length || conversationBusy) return;
    const sessionId = project.sessionId;
    const previousCursor = conversationBackStack.at(-1) ?? null;
    setConversationBusy(true);
    try {
      const page = await api.conversation(sessionId, previousCursor ?? undefined);
      if (activeSessionRef.current === sessionId) {
        setConversationBackStack((previous) => previous.slice(0, -1));
        setConversationCursor(previousCursor);
        setConversation(page.entries);
        setConversationNext(page.next_cursor);
      }
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      setConversationBusy(false);
    }
  };

  const showConversationBody = async (entry: ConversationEntry, offset = 0) => {
    if (!project.sessionId) return;
    const sessionId = project.sessionId;
    setConversationBusy(true);
    try {
      const page = await api.conversationBody(sessionId, entry, offset);
      if (activeSessionRef.current === sessionId) {
        setConversationDetail((previous) => ({
          entry,
          text: offset && previous?.entry.id === entry.id ? previous.text + page.text : page.text,
          nextOffset: page.next_offset,
        }));
      }
    } catch (cause) {
      if (activeSessionRef.current === sessionId) setNotice(String(cause));
    } finally {
      setConversationBusy(false);
    }
  };

  return (
    <div className={`shell ${sidebarOpen ? "" : "sidebar-collapsed"}`}>
      <aside className="sidebar" aria-label="导航与任务">
        <div className="brand"><span className="brand-mark">R</span><div><strong>Rexak Agent</strong><small>安全测试工作台</small></div></div>
        <button className="new-chat" onClick={newChat}><span>＋</span> 新聊天</button>
        <div className="side-heading"><span>项目</span><button title="新建项目" onClick={() => setShowProjectForm(true)}>＋</button></div>
        <select className="project-select" aria-label="当前项目" value={project.projectId} onChange={(event) => { newChat(); project.setProjectId(event.target.value); }}>
          <option value="">我的工作台（默认）</option>{project.projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <div className="side-scroll">
          <div className="side-heading sessions-heading"><span>最近任务</span><button title="新建测试任务" onClick={() => setShowSessionForm(true)}>＋</button></div>
          {project.sessions.map((item) => (
            <div key={item.id} className={`task-row ${project.sessionId === item.id ? "selected" : ""}`}>
              <button className="side-item session-link" onClick={() => { project.setSessionId(item.id); setTab("conversation"); }}>{item.title}</button>
              <button className="task-delete" aria-label={`删除任务 ${item.title}`} title="删除任务" onClick={() => setDeleteTarget(item)}>×</button>
            </div>
          ))}
          {project.projectId && project.sessions.length === 0 && <p className="empty-side">这个项目还没有任务。</p>}
        </div>
        <div className="sidebar-foot">本地个人工作台<br />结果以证据和复测为准</div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="header-title"><button className="icon-button" aria-label="切换侧栏" onClick={() => setSidebarOpen(!sidebarOpen)}>☰</button><h1>{project.sessionId ? snapshot?.session.title ?? "加载任务…" : "新聊天"}</h1></div>
          <div className="top-actions"><span className={`connection ${project.ready && (!project.sessionId || connection === "已连接") ? "online" : "offline"}`}>{!project.ready ? project.error ? "服务已断开" : "连接中" : project.sessionId ? connection : "已就绪"}</span>
            {currentRun && <span className={`run-badge status-${currentRun.status}`}>{label(currentRun.status)}</span>}
            {project.sessionId && <button className="details-toggle" onClick={() => { setShowDetails(!showDetails); if (showDetails) setTab("conversation"); }}>任务详情</button>}</div>
        </header>
        {(project.error || notice) && <div className="notice" role="status">{project.error || notice}<button onClick={() => { project.setError(""); setNotice(""); }}>×</button></div>}
        {!project.sessionId ? (
          <div className="welcome"><div className="welcome-icon">R</div><h2>有什么可以帮你？</h2><p>直接描述你的需求和目标网址或 IP，按你的指令开始测试。</p>
            <button onClick={() => setShowSessionForm(true)}>创建测试任务 ↗</button></div>
        ) : (
          <>
            {showDetails && <div className="summary-strip">
              <div><small>当前阶段</small><strong>{label(currentRun?.status)}</strong></div>
              <div><small>已确认发现</small><strong>{findings.filter((item) => item.status === "confirmed").length}</strong></div>
              <div><small>待验证线索</small><strong>{leads.filter((item) => !["closed", "resolved"].includes(item.status)).length}</strong></div>
              <div><small>已登记检查</small><strong>{snapshot?.coverage.total ?? "—"}</strong></div>
              <div><small>模型请求</small><strong>{usage?.call_count ?? "—"}</strong></div>
            </div>}
            <div className="run-controls">
              <span className="objective">{currentRun?.objective ?? "任务尚未开始"}</span>
              {currentRun && <div className="control-buttons">
                <button title="停止当前回复；发送新指令即可继续" disabled={busy || !["queued", "running", "waiting_tool", "waiting_user"].includes(currentRun.status)} onClick={() => void runAction(() => api.control(currentRun.id, "pause"), "正在停止当前回复，暂停后可直接发送新指令")}>暂停</button>
                <button disabled={busy || !["paused", "waiting_user", "recovering"].includes(currentRun.status)} onClick={() => void resumeCurrentRun()}>继续</button>
                <button className="danger" disabled={busy || ["completed", "cancelled", "failed"].includes(currentRun.status)} onClick={() => void runAction(() => api.control(currentRun.id, "cancel"), "取消指令已提交，仍需等待执行状态确认")}>取消</button>
              </div>}
            </div>
            {showDetails && <nav className="tabs" aria-label="任务内容">
              {([["activity", "实时活动"], ["conversation", "对话"], ["plan", "当前计划"], ["findings", "发现与覆盖"], ["jobs", "后台任务"], ["cost", "费用"], ["diagnostics", "耗时诊断"], ["reports", "报告"], ["settings", "设置"]] as const).map(([key, title]) =>
                <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{title}</button>
              )}
            </nav>}
            <div className="content" onScroll={(event) => {
              const view = event.currentTarget;
              followChatRef.current = view.scrollHeight - view.scrollTop - view.clientHeight < 100;
            }}>
              {tab === "activity" && <div className="activity-layout">
                <section className="panel"><div className="panel-head"><div><h2>活动记录</h2><p>{historyMode ? `历史第 ${historyBackStack.length + 1} 页；新事件仍在后台接收` : "按任务事件顺序排列；历史按页读取。"}</p></div><span className="muted">本页 {events.length} 条</span></div>
                  <VirtualEventList key={`${project.sessionId}:${historyBefore ?? "latest"}`} events={events} onSelect={(event) => void showEvent(event)} />
                  <div className="panel-footer">{historyMode && <button disabled={historyBusy} onClick={() => void loadNewer()}>较新一页</button>}{olderBefore !== null && <button disabled={historyBusy} onClick={() => void loadOlder()}>更早一页</button>}{historyMode && <button disabled={historyBusy} onClick={() => void backToLatest()}>返回最新</button>}</div>
                </section>
                <section className="panel assistant-panel"><div className="panel-head"><div><h2>最近的助手输出</h2><p>仅展示已收到的文本；完整证据从工件查看。</p></div></div>
                  <pre className="assistant-output">{assistantText || "等待助手输出…"}</pre>
                </section>
              </div>}
              {tab === "conversation" && <section className="chat-thread" aria-label="对话">
                {conversationBackStack.length > 0 && <div className="chat-pagination"><button onClick={() => void loadNewerConversation()}>较新一页</button><button onClick={() => void refreshConversation()}>返回最新</button></div>}
                {conversationNext !== null && <div className="chat-pagination"><button disabled={conversationBusy} onClick={() => void loadMoreConversation()}>查看更早的消息</button></div>}
                {!conversationCursor && snapshot?.runs[0]?.objective && !conversation.some((entry) => entry.role === "user" && entry.run_id === snapshot?.runs[0]?.id) && <article className="chat-message user" data-role="user"><div className="message-body"><p>{snapshot.runs[0].objective}</p></div></article>}
                {toolNext !== null && <div className="chat-pagination"><button disabled={toolBusy} onClick={() => void loadEarlierTools()}>查看更早的工具操作</button></div>}
                {toolError && <div className="chat-progress error">{toolError}</div>}
                {chatTimeline(conversation, toolActivities.filter((item) => !conversationCursor || (item.started_at >= (conversation.at(-1)?.occurred_at ?? "") && item.started_at <= (conversation[0]?.occurred_at ?? "")))).map((item) => item.kind === "message"
                  ? <ChatMessage key={`${item.entry.source}:${item.entry.id}`} sessionId={project.sessionId} entry={item.entry} />
                  : <ToolActivityGroup key={`${item.entries[0].run_id}:${item.entries[0].tool_call_id}`} sessionId={project.sessionId} items={item.entries} />)}
                {!!assistantText && ["running", "cancelling", "pausing", "paused"].includes(currentRun?.status ?? "") && !conversation.some((entry) => entry.role === "assistant" && entry.run_id === currentRun?.id && assistantStartedAt && Date.parse(entry.occurred_at) >= Date.parse(assistantStartedAt)) && <article className="chat-message assistant" data-role="assistant"><div className="message-body"><div className="markdown-text">{assistantText}</div></div></article>}
                {currentRun?.status === "paused" && <div className="chat-progress" role="status">已暂停。发送新指令即可继续，也可以新建聊天。</div>}
                {currentRun && !["completed", "cancelled", "failed", "waiting_user", "paused", "recovering"].includes(currentRun.status) && <div className="chat-progress" role="status"><span className="progress-dot" />{currentRun.status === "running" && modelProgressLabel ? modelProgressLabel : label(currentRun.status)}{currentRun.status === "queued" ? " · 等待运行器接手" : " · 可继续补充要求"}</div>}
                {currentRun?.status === "recovering" && <div className="chat-progress" role="status">运行已中断，等待恢复。点击“继续”可恢复已保存的任务。</div>}
                {["failed", "recovering"].includes(currentRun?.status ?? "") && <div className="chat-progress error">{currentRun?.stop_reason || "运行中断，请查看任务详情中的活动记录。"}</div>}
                <div ref={chatEndRef} />
              </section>}
              {tab === "plan" && <div className="stack">
                <section className="panel"><div className="panel-head"><div><h2>当前计划</h2><p>版本 {plan?.revision ?? "—"} · 从图状态读取；步骤可以随探索修订。</p></div><button onClick={() => void api.plan(project.sessionId).then(setPlan).catch((cause) => setNotice(String(cause)))}>刷新</button></div>
                  {plan?.steps.length ? plan.steps.map((step) => <article key={step.step_id} className="record"><div className="record-title"><strong>{step.step_id} · {step.description}</strong><span className={`pill status-${step.status}`}>{label(step.status)}</span></div><p>{step.rationale || "暂无说明"}</p>{step.lead_id && <small>关联线索：{step.lead_id}</small>}</article>) : <p className="empty">尚无计划步骤</p>}
                </section>
                <section className="panel"><div className="panel-head"><div><h2>步骤与线索关系</h2><p>只显示计划中明确填写的 lead_id 关联；位置和连线不表示依赖或执行顺序。</p></div></div>{plan && <PlanGraph plan={plan} leads={leads} />}</section>
              </div>}
              {tab === "findings" && <div className="stack">
                <section className="panel"><div className="panel-head"><div><h2>发现</h2><p>待验证结果不会计入已确认数量。</p></div></div>
                  {findings.length ? findings.map((item) => <article key={item.id} className="record"><div className="record-title"><strong>{item.title}</strong><span className={`pill status-${item.status}`}>{label(item.status)}</span></div>
                    <p>{item.description || "暂无说明"}</p><small>{item.category} · {item.id}</small>
                    {!!item.evidence_refs?.length && <div className="evidence-links">{item.evidence_refs.map((id) => <button key={id} onClick={() => void showEvidence(id)}>查看证据 {id.slice(0, 8)}</button>)}</div>}
                  </article>) : <p className="empty">尚无发现</p>}
                </section>
                <section className="panel"><div className="panel-head"><div><h2>待跟进线索</h2><p>优先级仅辅助排序，受阻与暂缓会保留原因。</p></div></div>
                  {leads.length ? leads.map((item) => <article key={item.id} className="record"><div className="record-title"><strong>{item.hypothesis}</strong><span className="pill">{label(item.status)}</span></div>
                    <p>{item.next_action || item.blocked_reason || "等待下一步"}</p><small>优先级 {item.priority}</small></article>) : <p className="empty">尚无线索</p>}
                </section>
                <section className="panel"><div className="panel-head"><h2>覆盖记录</h2></div>
                  <div className="coverage-grid">{Object.entries(snapshot?.coverage.by_status ?? {}).map(([key, count]) => <div key={key}><strong>{count}</strong><span>{label(key)}</span></div>)}</div>
                  <p className="fineprint">{snapshot?.coverage.ratio_definition || "尚未登记覆盖；不能据此推断目标没有漏洞。"}</p>
                </section>
              </div>}
              {tab === "jobs" && <section className="panel"><div className="panel-head"><div><h2>后台任务</h2><p>远端状态未知时不会显示为已完成。</p></div></div>
                {jobs.length ? jobs.map((item) => <article key={item.job_id} className="record compact"><div className="record-title"><strong>{item.job_id.slice(0, 12)}…</strong><span className={`pill status-${item.state}`}>{label(item.state)}</span></div>
                  <p>同步状态：{label(item.sync_status)}{item.cancel_requested ? " · 已请求取消" : ""}{item.cancel_confirmed ? " · 已确认取消" : ""}</p><small>上次同步：{time(item.last_synced_at)}</small></article>) : <p className="empty">目前没有后台任务</p>}
              </section>}
              {tab === "cost" && <div className="stack"><section className="panel"><div className="panel-head"><div><h2>费用与用量</h2><p>已知费用按币种分开；未知请求不会按零元计算。</p></div></div>
                <div className="metric-row"><div><small>模型请求</small><strong>{usage?.call_count ?? "—"}</strong></div><div><small>未定价</small><strong>{usage?.unpriced_calls ?? "—"}</strong></div><div><small>用量未知</small><strong>{usage?.unknown_usage_calls ?? "—"}</strong></div></div>
                <div className="cost-currencies">{Object.entries(usage?.by_currency ?? {}).map(([currency, bucket]) => <div key={currency}><strong>{bucket.known_cost} {currency}</strong><small>{bucket.priced_calls} 次已定价请求</small></div>)}{!Object.keys(usage?.by_currency ?? {}).length && <p className="empty">暂无可计算费用；已知 token 仍在下方显示。</p>}</div>
                <div className="token-grid">{Object.entries(usage?.token_totals ?? {}).map(([key, value]) => <div key={key}><small>{label(key)}</small><strong>{value === null ? "未知" : value.toLocaleString()}</strong></div>)}</div>
                <p className="fineprint">{usage?.note}</p>
              </section><section className="panel"><div className="panel-head"><h2>模型调用时间线</h2></div>
                {modelCalls.length ? modelCalls.map((item) => <div key={item.model_call_id} className="timeline-row"><div><strong>{item.phase}</strong><small>{item.model_name} · {time(item.submitted_at)}</small></div><div className="timeline-right"><span>{item.input_tokens ?? "?"} 输入 / {item.output_tokens ?? "?"} 输出</span><strong>{item.cost_amount === null ? label(item.cost_status) : `${item.cost_amount} ${item.cost_currency ?? ""}`}</strong></div></div>) : <p className="empty">暂无模型调用</p>}
              </section></div>}
              {tab === "diagnostics" && <section className="panel"><div className="panel-head"><div><h2>模型、工具与结束判定时间线</h2><p>按控制端记录时间排列，便于定位等待区间；远端执行耗时须结合 Executor 台账判断。</p></div><span className="muted">最近 {diagnosticRows.length} 条</span></div>
                {diagnosticRows.length ? diagnosticRows.map((row) => row.kind === "model" ? (
                  <div key={`model-${row.item.model_call_id}`} className="diagnostic-row"><div><span className="diagnostic-kind">模型</span><strong>{row.item.model_name}</strong><small>第 {row.item.retry_index + 1} 次尝试 · {time(row.item.submitted_at)}{row.item.error_code ? ` · ${row.item.error_code}` : ""}</small></div><div className="diagnostic-metrics"><strong>{row.item.completed_at ? duration(row.item.submitted_at, row.item.completed_at) : "等待模型返回"}</strong><small>首个可见分片：{row.item.first_token_at ? duration(row.item.submitted_at, row.item.first_token_at) : "未知"}</small></div></div>
                ) : row.kind === "tool" ? (
                  <div key={`tool-${row.item.operation_id}`} className="diagnostic-row"><div><span className="diagnostic-kind tool">工具</span><strong>{row.item.tool_name}</strong><small>{time(row.item.created_at)} · 控制端{label(row.item.status)}{row.item.job_state ? ` · 执行岛${label(row.item.job_state)}` : ""}{row.item.sync_status && row.item.sync_status !== "in_sync" ? ` · ${label(row.item.sync_status)}` : ""}</small></div><div className="diagnostic-metrics"><strong>控制端跨度：{["accepted", "running"].includes(row.item.status) ? "操作尚未结束" : duration(row.item.created_at, row.item.updated_at)}</strong>{row.item.job_state && <small>执行岛排队：{duration(row.item.executor_accepted_at, row.item.executor_started_at)}</small>}{row.item.job_state && <small>执行岛执行：{duration(row.item.executor_started_at, row.item.executor_finished_at)}</small>}{nextModelGap(row.item) && <small>至同一阶段下一次模型请求：{nextModelGap(row.item)}</small>}</div></div>
                ) : (
                  <div key={`verification-${row.item.id}`} className="diagnostic-row"><div><span className="diagnostic-kind verification">阶段耗时</span><strong>{spanTitles[row.item.name] ?? row.item.name}</strong><small>{time(row.item.started_at)} · {row.item.error ? `处理异常：${row.item.error}` : row.item.decision ? `结果：${label(row.item.decision)}` : row.item.status ? `状态：${label(row.item.status)}` : "结果未知"}</small></div><div className="diagnostic-metrics"><strong>{row.item.duration_ms === null ? "耗时未知" : row.item.duration_ms < 1000 ? `${row.item.duration_ms.toFixed(0)} 毫秒` : `${(row.item.duration_ms / 1000).toFixed(2)} 秒`}</strong></div></div>
                )) : <p className="empty">还没有模型、工具或阶段耗时记录</p>}
                <p className="fineprint">控制端跨度是受理到最后一次状态更新；模型首个可见分片只在分片与调用的消息 ID 精确匹配时计算，不等于供应商首字节时间。执行岛排队与执行分别由同一 Executor 台账的时间戳计算，缺少时间戳即显示未知。Git HEAD 复测调用返回只计工具受理或返回前的等待，后台任务耗时看任务记录。结束判定与报告生成分别计时；下一次模型请求间隔只按时间和阶段匹配，不能单凭它判断耗时原因。每类最多读取最近 100 条。</p>
              </section>}
              {tab === "reports" && <section className="panel"><div className="panel-head"><div><h2>报告版本</h2><p>每次生成保留历史；确认项须有可核验的证据链。</p></div><button className="primary" disabled={busy} onClick={() => void runAction(() => api.createReport(project.sessionId), "报告版本已生成")}>生成新版本</button></div>
                {reports.length ? reports.map((item) => <article key={item.version} className="record compact"><div className="record-title"><strong>版本 {item.version}</strong><span className="pill">{label(item.status)}</span></div><small>{time(item.created_at)}{item.is_historical ? " · 历史版本" : " · 当前版本"}</small>
                  {item.status === "completed" && <div className="evidence-links"><a href={`/api/sessions/${project.sessionId}/reports/${item.version}/export?format=markdown`}>下载 Markdown</a><a href={`/api/sessions/${project.sessionId}/reports/${item.version}/export?format=html`}>下载 HTML</a></div>}
                </article>) : <p className="empty">还没有报告版本</p>}
              </section>}
              {tab === "settings" && <div className="stack"><section className="panel"><div className="panel-head"><h2>模型配置</h2></div><p className="fineprint">{modelSettings?.effective_scope.replaceAll("**", "") ?? "读取配置中…"}</p>
                {modelSettings?.profiles.map((item) => <div className="settings-row" key={`${item.provider_id}/${item.model_name}`}><div><strong>{item.provider_id} / {item.model_name}</strong><small>上下文 {item.context_limit.toLocaleString()} · 配置版本 {item.profile_version}</small></div><span className={`pill ${item.secret_configured ? "status-confirmed" : "status-blocked"}`}>{item.secret_configured ? "密钥已配置" : "缺少密钥"}</span></div>)}
                <p className="fineprint">价格目录版本：{modelSettings?.price_catalog_version ?? "未知"}。活动阶段继续使用启动时的配置快照。</p>
              </section><section className="panel"><div className="panel-head"><div><h2>模型价格</h2><p>填写供应商实际价格；未配置时费用保持“未定价”。</p></div><button disabled={busy} onClick={() => void api.priceCatalog().then(setPriceCatalog).catch((cause) => setNotice(String(cause)))}>刷新版本</button></div>
                <div className="price-grid">
                  <label>模型<select value={priceProfileIndex} onChange={(event) => setPriceProfileIndex(Number(event.target.value))}>{modelSettings?.profiles.map((item, index) => <option key={`${item.provider_id}/${item.model_name}`} value={index}>{item.provider_id} / {item.model_name}</option>)}</select></label>
                  <label>货币代码<input value={priceForm.currency} maxLength={3} onChange={(event) => setPriceForm((previous) => ({ ...previous, currency: event.target.value.toUpperCase() }))} placeholder="CNY" /></label>
                  <label>输入 / 百万 token<input value={priceForm.input} inputMode="decimal" onChange={(event) => setPriceForm((previous) => ({ ...previous, input: event.target.value }))} placeholder="例如 1.25" /></label>
                  <label>输出 / 百万 token<input value={priceForm.output} inputMode="decimal" onChange={(event) => setPriceForm((previous) => ({ ...previous, output: event.target.value }))} placeholder="例如 5.00" /></label>
                  <label>缓存读取 / 百万 token（可空）<input value={priceForm.cacheRead} inputMode="decimal" onChange={(event) => setPriceForm((previous) => ({ ...previous, cacheRead: event.target.value }))} /></label>
                  <label>缓存写入 / 百万 token（可空）<input value={priceForm.cacheWrite} inputMode="decimal" onChange={(event) => setPriceForm((previous) => ({ ...previous, cacheWrite: event.target.value }))} /></label>
                </div>
                <div className="price-flags"><label><input type="checkbox" checked={priceForm.cacheIncluded} onChange={(event) => setPriceForm((previous) => ({ ...previous, cacheIncluded: event.target.checked }))} /> 供应商输入 token 已包含缓存读取</label><label><input type="checkbox" checked={priceForm.reasoningIncluded} onChange={(event) => setPriceForm((previous) => ({ ...previous, reasoningIncluded: event.target.checked }))} /> 供应商输出 token 已包含推理 token</label></div>
                <div className="panel-footer"><button className="primary" disabled={busy || !priceCatalog || !selectedPriceProfile} onClick={() => void savePrice()}>保存价格版本</button></div>
                <p className="fineprint">仅影响之后启动的运行；历史调用按当时版本保留。这里是应用侧费用估算，不代表供应商账单。</p>
              </section><section className="panel"><div className="panel-head"><h2>运行模式与目标</h2></div><p>{snapshot?.session.scope?.mode === "scoped" ? "严格范围模式" : "按指令运行"}</p><p>当前目标：{snapshot?.session.scope?.rules?.map((rule) => rule.host).join("、") || "请在聊天里提供目标网址或 IP"}</p><p className="fineprint">按指令运行时，聊天里提供的目标会自动应用；严格范围模式按预先设置的规则执行。</p><details><summary>查看目标规则</summary><pre className="scope-view">{JSON.stringify(snapshot?.session.scope ?? {}, null, 2)}</pre></details></section></div>}
            </div>
          </>
        )}
        <div className="composer-wrap"><div className="composer">
          <textarea aria-label="消息" placeholder="发送消息，描述测试目标或补充要求…" value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void sendMessage(); }
          }} />
          <div className="composer-footer"><button className="composer-plus" title="创建测试任务" aria-label="设置测试任务" onClick={() => setShowSessionForm(true)}>＋</button><span>{project.sessionId && usage ? `${usage.call_count} 次请求 · ${Object.entries(usage.by_currency).map(([currency, value]) => `${value.known_cost} ${currency}`).join(" / ") || "费用未定价"}${usage.unknown_usage_calls ? " · 存在未知用量" : ""}` : "Enter 发送，Shift+Enter 换行"}</span><button className="send-button" aria-label="发送消息" disabled={busy || !project.ready || !message.trim()} onClick={() => void sendMessage()}>↑</button></div>
        </div><p className="composer-note">测试结论以证据和复测为准</p></div>
      </main>

      {showProjectForm && <div className="modal-backdrop" onMouseDown={() => setShowProjectForm(false)}><div className="modal" role="dialog" aria-modal="true" aria-label="新建项目" onMouseDown={(event) => event.stopPropagation()}><h2>新建项目</h2><p>同一目标的任务、经验与报告会归在这个项目中。</p>
        <label>项目名称<input value={projectName} onChange={(event) => setProjectName(event.target.value)} autoFocus /></label><label>目标说明<textarea value={projectDescription} onChange={(event) => setProjectDescription(event.target.value)} /></label>
        <div className="modal-actions"><button onClick={() => setShowProjectForm(false)}>取消</button><button className="primary" disabled={busy} onClick={() => void submitProject()}>创建项目</button></div></div></div>}
      {showSessionForm && <div className="modal-backdrop" onMouseDown={() => setShowSessionForm(false)}><div className="modal" role="dialog" aria-modal="true" aria-label="新建测试任务" onMouseDown={(event) => event.stopPropagation()}><h2>新建测试任务</h2><p>直接在指令里描述目标即可，网址也可以在后续聊天中补充。</p>
        <label>运行模式<select aria-label="运行模式" value={runMode} onChange={(event) => setRunMode(event.target.value as "instruction" | "scoped")}><option value="instruction">按指令运行（默认）</option><option value="scoped">严格范围模式</option></select></label>
        <label>任务名称<input value={sessionTitle} onChange={(event) => setSessionTitle(event.target.value)} autoFocus placeholder="例如：登录与权限边界检查" /></label>
        <label>测试目标<textarea value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="希望它自主检查什么、何时可以停下" /></label>
        <label>目标网址（可选）<input value={targetUrl} onChange={(event) => setTargetUrl(event.target.value)} placeholder="https://example.test/" /></label>
        <p className="fineprint">{runMode === "instruction" ? "自动读取用户指令中的目标，允许该主机的协议和端口探索，无需手填 scope。" : "按填写的网址固定协议、主机和端口。"}目标须能从执行端访问。</p>
        <div className="modal-actions"><button onClick={() => setShowSessionForm(false)}>取消</button><button className="primary" disabled={busy} onClick={() => void submitSession()}>创建并开始</button></div></div></div>}
      {showRecoveryConfirm && currentRun && <div className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-label="确认恢复"><h2>核对后恢复</h2><p>{currentRun.stop_reason || "上一次请求的状态未能完全确认。"}</p><p>恢复前请核对工具结果与可能产生的费用。确认后将继续当前任务，不会自动重做未知的工具操作。</p><div className="modal-actions"><button onClick={() => setShowRecoveryConfirm(false)}>暂不恢复</button><button className="primary" disabled={busy} onClick={() => void runAction(async () => { await api.control(currentRun.id, "resume", true); setShowRecoveryConfirm(false); }, "恢复指令已提交")}>确认并继续</button></div></div></div>}
      {deleteTarget && <div className="modal-backdrop" onMouseDown={() => !busy && setDeleteTarget(null)}><div className="modal" role="dialog" aria-modal="true" aria-label="删除任务" onMouseDown={(event) => event.stopPropagation()}><h2>删除任务？</h2><p>“{deleteTarget.title}”将从列表移除。正在执行的任务会请求取消；历史审计记录保留。</p><div className="modal-actions"><button disabled={busy} onClick={() => setDeleteTarget(null)}>保留</button><button className="danger" disabled={busy} onClick={() => void removeTask()}>删除</button></div></div></div>}
      {preview && <div className="modal-backdrop" onMouseDown={() => setPreview(null)}><div className="modal wide" role="dialog" aria-modal="true" aria-label="证据预览" onMouseDown={(event) => event.stopPropagation()}><h2>证据预览</h2><p>{preview.media_type} · {preview.byte_size.toLocaleString()} 字节</p><pre className="evidence-preview">{preview.preview}</pre><p className="fineprint">{preview.note}</p><div className="modal-actions"><button onClick={() => setPreview(null)}>关闭</button></div></div></div>}
      {eventDetail && <div className="modal-backdrop" onMouseDown={() => setEventDetail(null)}><div className="modal wide" role="dialog" aria-modal="true" aria-label="事件详情" onMouseDown={(event) => event.stopPropagation()}><h2>{label(eventDetail.type)} · #{eventDetail.session_seq}</h2><p>{time(eventDetail.occurred_at)} · 运行 {eventDetail.run_id ?? "未知"}</p><pre className="evidence-preview">{JSON.stringify(eventDetail.payload, null, 2)}</pre><p className="fineprint">此处显示服务端脱敏后的事件内容；工具原始输出请从工件预览查看。</p><div className="modal-actions"><button onClick={() => setEventDetail(null)}>关闭</button></div></div></div>}
      {conversationDetail && <div className="modal-backdrop" onMouseDown={() => setConversationDetail(null)}><div className="modal wide" role="dialog" aria-modal="true" aria-label="对话正文" onMouseDown={(event) => event.stopPropagation()}><h2>{conversationDetail.entry.role === "user" ? "你的指令" : "助手正文"}</h2><p>{time(conversationDetail.entry.occurred_at)}</p><pre className="evidence-preview">{conversationDetail.text}</pre><p className="fineprint">服务端按完整正文脱敏后分段提供。此归档只供回看，不参与 Agent 记忆。</p><div className="modal-actions">{conversationDetail.nextOffset !== null && <button disabled={conversationBusy} onClick={() => void showConversationBody(conversationDetail.entry, conversationDetail.nextOffset ?? 0)}>加载后续正文</button>}<button onClick={() => setConversationDetail(null)}>关闭</button></div></div></div>}
    </div>
  );
}
