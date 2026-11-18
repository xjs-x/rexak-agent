import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiRequestError,
  type AgentEvent,
  type ExecutionMode,
  type ConversationEntry,
  type FindingItem,
  type LeadItem,
  type ModelSettings,
  type PlanRead,
  type ProjectItem,
  type SessionItem,
  type Snapshot,
  type ToolActivityItem,
  type UsageSummary,
  type WeaknessItem,
} from "../api/client";
import { TERMINAL, latestRun } from "./labels";
import { appendAssistantDelta, assistantDelta, restoreAssistantDraft, type AssistantDraft } from "./assistantStream";

const EVENT_TYPES = [
  "skill.activity",
  "run.status",
  "message.delta",
  "message.completed",
  "plan.updated",
  "focus.updated",
  "leads.recalled",
  "context.selected",
  "segment.yielded",
  "snapshot.captured",
  "model.response_quality",
  "tool.accepted",
  "tool.output",
  "tool.completed",
  "lead.updated",
  "weakness.changed",
  "weakness.presented",
  "weakness.reviewed",
  "relation.changed",
  "condition.review_due",
  "completion.review",
  "finding.updated",
  "coverage.updated",
  "usage.updated",
  "memory.compacted",
  "input.required",
  "report.ready",
  "error",
  "stream.resync_required",
];

export type ConnectionState = "connecting" | "online" | "offline";
export type ExecutorState = "unknown" | "checking" | "online" | "offline";

export interface ProjectGroup extends ProjectItem {
  sessions: SessionItem[];
}

export interface ModelOption {
  key: string;
  provider: string;
  providerLabel: string;
  model: string;
  displayName?: string;
  configured: boolean;
  isDefault: boolean;
}

export interface DeleteResult {
  ok: boolean;
  pending?: boolean;
  blocked?: boolean;
  message?: string;
  resources?: string[];
}

export interface WorkbenchApi {
  executionMode: ExecutionMode;
  ready: boolean;
  attachmentsEnabled: boolean;
  bootError: string;
  connection: ConnectionState;
  projects: ProjectGroup[];
  recent: SessionItem[];
  standalone: SessionItem[];
  sessionId: string;
  selectedProjectId: string;
  pendingProjectId: string | null;
  defaultProjectId: string;
  session: Snapshot["session"] | null;
  snapshot: Snapshot | null;
  run: Snapshot["runs"][number] | null;
  conversation: ConversationEntry[];
  tools: ToolActivityItem[];
  plan: PlanRead | null;
  weaknesses: WeaknessItem[];
  findings: FindingItem[];
  leads: LeadItem[];
  usage: UsageSummary | null;
  events: AgentEvent[];
  inputRequired: AgentEvent | null;
  assistantDraft: AssistantDraft | null;
  assistantModelCallId: string | undefined;
  lastActivityAt: string | undefined;
  modelPhase: string | null;
  hasMoreMessages: boolean;
  hasMoreTools: boolean;
  loadingMore: boolean;
  busy: boolean;
  notice: string;
  modelSettings: ModelSettings | null;
  modelOptions: ModelOption[];
  selectedModelKey: string;
  currentModelKey: string;
  modelLocked: boolean;
  setSelectedModelKey: (key: string) => void;
  skillCount: number | null;
  executorState: ExecutorState;
  executorId: string | null;
  ackRun: Snapshot["runs"][number] | null;
  selectSession: (id: string) => void;
  newChat: (projectId?: string | null) => void;
  setSelectedProjectId: (id: string) => void;
  setPendingProjectId: (id: string | null) => void;
  setNotice: (value: string) => void;
  refresh: () => Promise<void>;
  refreshProjects: () => Promise<void>;
  refreshProject: (projectId: string | null) => Promise<void>;
  reconnect: () => Promise<void>;
  ensureProjectSessions: (projectId: string) => Promise<void>;
  isProjectLoaded: (projectId: string) => boolean;
  createProject: (name: string, description: string) => Promise<ProjectItem>;
  send: (text: string, attachments?: string[], attachmentSession?: string, messageKey?: string, skillRefs?: string[]) => Promise<boolean>;
  prepareAttachmentSession: () => Promise<string>;
  control: (action: "pause" | "resume" | "cancel", acknowledge?: boolean) => Promise<boolean>;
  clearAck: () => void;
  resumeWithAcknowledgement: () => Promise<boolean>;
  loadOlderMessages: () => Promise<void>;
  loadMoreTools: () => Promise<void>;
  deleteSession: (session: SessionItem, terminateResources?: boolean) => Promise<DeleteResult>;
  reloadModels: () => void;
  onModelsSaved: (settings: ModelSettings) => void;
}

function mergeConversation(previous: ConversationEntry[], next: ConversationEntry[]): ConversationEntry[] {
  if (!next.length) return previous;
  const map = new Map(previous.map((entry) => [`${entry.source}:${entry.id}`, entry]));
  let changed = false;
  for (const entry of next) {
    const key = `${entry.source}:${entry.id}`;
    const old = map.get(key);
    const signature = `${entry.role}|${entry.preview}|${entry.has_more}|${entry.available}|${entry.response_outcome}|${entry.occurred_at}`;
    const oldSignature = old
      ? `${old.role}|${old.preview}|${old.has_more}|${old.available}|${old.response_outcome}|${old.occurred_at}`
      : null;
    if (!old || signature !== oldSignature) {
      map.set(key, entry);
      changed = true;
    }
  }
  if (!changed) return previous;
  return [...map.values()].sort((left, right) => Date.parse(left.occurred_at) - Date.parse(right.occurred_at));
}

function mergeTools(previous: ToolActivityItem[], next: ToolActivityItem[]): ToolActivityItem[] {
  if (!next.length) return previous;
  const map = new Map(previous.map((item) => [`${item.run_id}:${item.tool_call_id}`, item]));
  let changed = false;
  for (const item of next) {
    const key = `${item.run_id}:${item.tool_call_id}`;
    const old = map.get(key);
    const signature = `${item.status}|${item.finished_at}|${item.job_state}|${item.sync_status}`;
    const oldSignature = old
      ? `${old.status}|${old.finished_at}|${old.job_state}|${old.sync_status}`
      : null;
    if (!old || signature !== oldSignature) {
      map.set(key, item);
      changed = true;
    }
  }
  if (!changed) return previous;
  return [...map.values()].sort((left, right) => Date.parse(right.started_at) - Date.parse(left.started_at));
}

function mergeEvents(previous: AgentEvent[], next: AgentEvent[]): AgentEvent[] {
  if (!next.length) return previous;
  const map = new Map(previous.map((event) => [event.session_seq, event]));
  for (const event of next) map.set(event.session_seq, event);
  return [...map.values()].sort((left, right) => left.session_seq - right.session_seq).slice(-300);
}

function sessionKey(projects: ProjectGroup[], id: string): string {
  return projects.find((project) => project.sessions.some((session) => session.id === id))?.id ?? "";
}

export function useWorkbench(): WorkbenchApi {
  const [ready, setReady] = useState(false);
  const [attachmentsEnabled, setAttachmentsEnabled] = useState(false);
  const [bootError, setBootError] = useState("");
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [standalone, setStandalone] = useState<SessionItem[]>([]);
  const [projects, setProjects] = useState<ProjectGroup[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("ultra");
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [pendingProjectId, setPendingProjectId] = useState<string | null>(null);
  const [session, setSession] = useState<Snapshot["session"] | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [conversation, setConversation] = useState<ConversationEntry[]>([]);
  const [tools, setTools] = useState<ToolActivityItem[]>([]);
  const [plan, setPlan] = useState<PlanRead | null>(null);
  const [weaknesses, setWeaknesses] = useState<WeaknessItem[]>([]);
  const [findings, setFindings] = useState<FindingItem[]>([]);
  const [leads, setLeads] = useState<LeadItem[]>([]);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [assistantDraft, setAssistantDraft] = useState<AssistantDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNoticeState] = useState("");
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [selectedModelKey, setSelectedModelKey] = useState("");
  const [skillCount, setSkillCount] = useState<number | null>(null);
  const [executorState, setExecutorState] = useState<ExecutorState>("unknown");
  const [executorId, setExecutorId] = useState<string | null>(null);
  const [ackRun, setAckRun] = useState<Snapshot["runs"][number] | null>(null);
  const [hasMoreMessages, setHasMoreMessages] = useState(false);
  const [hasMoreTools, setHasMoreTools] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const genRef = useRef(0);
  const refreshRef = useRef<(() => Promise<void>) | null>(null);
  const conversationNextRef = useRef<string | null>(null);
  const toolNextRef = useRef<number | null>(null);
  const bootstrappedRef = useRef(false);
  const loadedProjectsRef = useRef<Set<string>>(new Set());
  const preferredSessionRef = useRef(new URLSearchParams(window.location.search).get("session") ?? "");
  const [modelEpoch, setModelEpoch] = useState(0);

  const setNotice = useCallback((value: string) => {
    setNoticeState(value);
  }, []);

  // 项目很多时（评测/夹具库），启动只为最近 8 个项目取会话；其余展开时再读。
  const ensureProjectSessions = useCallback(async (projectId: string) => {
    if (!projectId || loadedProjectsRef.current.has(projectId)) return;
    loadedProjectsRef.current.add(projectId);
    try {
      const page = { sessions: await api.allSessions(projectId) };
      setProjects((previous) =>
        previous.map((project) => (project.id === projectId ? { ...project, sessions: page.sessions } : project)),
      );
    } catch {
      loadedProjectsRef.current.delete(projectId);
    }
  }, []);

  const projectGroups = useMemo<ProjectGroup[]>(
    () => projects.map((project) => ({ ...project, sessions: project.sessions })),
    [projects],
  );

  const allSessions = useMemo(
    () => [...standalone, ...projectGroups.flatMap((project) => project.sessions)],
    [projectGroups, standalone],
  );

  const recent = useMemo(
    () => [...allSessions].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at)).slice(0, 30),
    [allSessions],
  );

  const defaultProjectId =
    selectedProjectId ||
    projectGroups.find((project) => project.name === "我的工作台")?.id ||
    projectGroups[0]?.id ||
    "";

  const run = latestRun(snapshot);
  const modelLocked = !!sessionId && !!run && !TERMINAL.has(run.status);
  const currentModelKey = modelLocked && run
    ? `${run.provider_id}/${run.model_name}`
    : selectedModelKey;
  const modelOptions = useMemo<ModelOption[]>(
    () => {
      const providers = new Map(modelSettings?.providers?.map((provider) => [
        provider.provider_id, {
          name: provider.display_name,
          models: new Map(provider.models?.map((model) => [model.model_id, model.display_name])),
        },
      ]));
      return (modelSettings?.profiles ?? []).map((profile) => ({
        key: `${profile.provider_id}/${profile.model_name}`,
        provider: profile.provider_id,
        providerLabel: providers.get(profile.provider_id)?.name ?? profile.provider_id,
        model: profile.model_name,
        displayName: providers.get(profile.provider_id)?.models.get(profile.model_name),
        configured: profile.secret_configured,
        isDefault:
          profile.provider_id === modelSettings?.default_provider &&
          profile.model_name === modelSettings?.default_model,
      }));
    },
    [modelSettings],
  );

  const inputRequired = useMemo(() => {
    if (!run) return null;
    const candidates = events.filter(
      (event) => event.type === "input.required" && (!event.run_id || event.run_id === run.id),
    );
    return candidates.at(-1) ?? null;
  }, [events, run]);

  const { lastActivityAt, assistantModelCallId } = useMemo(() => {
    const recent = [...events].reverse();
    const activity = recent.find(event => event.run_id === run?.id &&
      ["run.status", "tool.accepted", "tool.output", "tool.completed", "usage.updated"].includes(event.type));
    const quality = assistantDraft?.responseId && recent.find(event =>
      event.run_id === assistantDraft.runId && event.type === "model.response_quality" &&
      event.payload?.response_id === assistantDraft.responseId);
    const callId = quality ? quality.payload?.model_call_id : undefined;
    return { lastActivityAt: activity?.occurred_at,
      assistantModelCallId: typeof callId === "string" ? callId : undefined };
  }, [events, run?.id, assistantDraft?.responseId, assistantDraft?.runId]);

  const modelPhase = useMemo(() => {
    if (!run) return null;
    const event = events
      .filter(
        (item) =>
          item.type === "run.status" &&
          item.run_id === run.id &&
          (item.payload as Record<string, unknown> | undefined)?.scope === "model_request",
      )
      .at(-1);
    const phase = String((event?.payload as Record<string, unknown> | undefined)?.phase ?? "");
    if (!phase || phase === "finished") return null;
    return {
      waiting: "等待模型响应",
      retry_wait: "模型连接暂时中断，正在自动等待重连",
      thinking: "模型正在思考",
      responding: "模型正在生成回复或工具调用",
    }[phase] ?? "模型请求进行中";
  }, [events, run]);

  /* ---------------- 启动：真实项目与会话 ---------------- */
  useEffect(() => {
    let active = true;
    let timer: number | undefined;

    const load = async () => {
      try {
        const supportsAttachments = await api.health();
        if (active) setAttachmentsEnabled(supportsAttachments);
        await api.signIn();
        if (!bootstrappedRef.current) {
          const data = await api.projects();
          const ungrouped = await api.allSessions(null);
          if (active) setStandalone(ungrouped);
          const initial = data.projects.slice(0, 8);
          const lists = await Promise.all(
            initial.map(async (project) => {
              try {
                const page = { sessions: await api.allSessions(project.id) };
                return [project.id, page.sessions] as const;
              } catch {
                return [project.id, [] as SessionItem[]] as const;
              }
            }),
          );
          if (!active) return;
          const map = Object.fromEntries(lists);
          for (const project of initial) loadedProjectsRef.current.add(project.id);
          setProjects(data.projects.map((project) => ({ ...project, sessions: map[project.id] ?? [] })));
          bootstrappedRef.current = true;
          setBootError("");
          setReady(true);
          setConnection("online");
        } else {
          setBootError("");
          setReady(true);
          setConnection("online");
        }
      } catch (cause) {
        if (!active) return;
        setBootError(String(cause instanceof Error ? cause.message : cause));
        setConnection("offline");
        setReady(false);
        timer = window.setTimeout(() => void load(), 5000);
      }
    };

    const lost = () => {
      setConnection("offline");
      setNotice("本机服务已断开。页面会继续保留当前输入，恢复后自动重连。");
    };
    window.addEventListener("workbench:connection-lost", lost);
    void load();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
      window.removeEventListener("workbench:connection-lost", lost);
    };
  }, [setNotice]);

  /* ---------------- 设置、技能数量 ---------------- */
  useEffect(() => {
    if (!ready) return;
    void api.modelSettings().then(setModelSettings).catch((cause) => setNotice(String(cause)));
  }, [ready, modelEpoch, setNotice]);

  useEffect(() => {
    if (!ready) return;
    void api.skills("").then((page) => setSkillCount(page.total)).catch(() => setSkillCount(null));
  }, [ready]);

  // 选定模型：默认跟随 settings 默认；打开历史会话时跟随该 run 的模型。
  useEffect(() => {
    if (!modelSettings) return;
    const keys = modelSettings.profiles.map((profile) => `${profile.provider_id}/${profile.model_name}`);
    if (!selectedModelKey || !keys.includes(selectedModelKey)) {
      setSelectedModelKey(`${modelSettings.default_provider}/${modelSettings.default_model}`);
    }
  }, [modelSettings, selectedModelKey]);

  useEffect(() => {
    if (sessionId && run?.provider_id && run?.model_name) {
      setSelectedModelKey(`${run.provider_id}/${run.model_name}`);
    }
  }, [run?.model_name, run?.provider_id, sessionId]);

  /* ---------------- URL 恢复 ---------------- */
  useEffect(() => {
    if (!ready || sessionId) return;
    const preferred = preferredSessionRef.current;
    if (!preferred) return;
    preferredSessionRef.current = "";
    setSessionId(preferred);
  }, [ready, sessionId]);

  // 快照回来后才知道会话属于哪个项目；按需补该项目的会话列表。
  useEffect(() => {
    if (!ready || !session?.project_id) return;
    setSelectedProjectId(session.project_id);
    void ensureProjectSessions(session.project_id);
  }, [ensureProjectSessions, ready, session?.project_id]);

  /* ---------------- 选中会话：真实数据 + SSE ---------------- */
  useEffect(() => {
    if (!ready || !sessionId) {
      setSession(null);
      setSnapshot(null);
      setConversation([]);
      setTools([]);
      setPlan(null);
      setWeaknesses([]);
      setFindings([]);
      setLeads([]);
      setUsage(null);
      setEvents([]);
      setAssistantDraft(null);
      conversationNextRef.current = null;
      toolNextRef.current = null;
      setHasMoreMessages(false);
      setHasMoreTools(false);
      return;
    }

    const session = sessionId;
    const generation = ++genRef.current;
    const guard = (fn: () => void) => {
      if (generation === genRef.current) fn();
    };
    let closed = false;
    let source: EventSource | null = null;
    let refreshTimer: number | undefined;
    let watchdog: number | undefined;
    let lastSeq = 0;
    let lastEventAt = Date.now();
    let recoveryPending = false;
    let loaded = false;
    let refreshing = false;
    let refreshRequested = false;

    setConnection("connecting");
    setSnapshot(null);
    setConversation([]);
    setTools([]);
    setPlan(null);
    setWeaknesses([]);
    setFindings([]);
    setLeads([]);
    setUsage(null);
    setEvents([]);
    setAssistantDraft(null);
    conversationNextRef.current = null;
    toolNextRef.current = null;
    setHasMoreMessages(false);
    setHasMoreTools(false);

    const applySnapshot = (value: Snapshot) => {
      guard(() => {
        setSnapshot((previous) =>
          previous && previous.session.id === value.session.id && previous.event_cursor > value.event_cursor
            ? previous
            : value,
        );
        setSession(value.session);
      });
    };

    const applyConversation = (page: Awaited<ReturnType<typeof api.conversation>>) => {
      guard(() => {
        setConversation((previous) => mergeConversation(previous, page.entries));
        if (conversationNextRef.current === null) {
          conversationNextRef.current = page.next_cursor;
          setHasMoreMessages(!!page.next_cursor);
        }
      });
    };

    const applyTools = (page: Awaited<ReturnType<typeof api.toolActivities>>) => {
      guard(() => {
        setTools((previous) => mergeTools(previous, page.activities));
        if (toolNextRef.current === null) {
          toolNextRef.current = page.next_offset;
          setHasMoreTools(page.next_offset !== null);
        }
      });
    };

    const refresh = async () => {
      if (closed) return;
      if (refreshing) {
        refreshRequested = true;
        return;
      }
      refreshing = true;
      try {
        const results = await Promise.allSettled([
          api.snapshot(session),
          api.plan(session),
          api.toolActivities(session),
          api.weaknesses(session),
          api.leads(session),
          api.usage(session),
          api.conversation(session),
          api.eventPage(session),
        ]);
        if (closed || generation !== genRef.current) return;
        const [snap, currentPlan, activity, weak, lead, cost, chat, eventPage] = results;
        if (snap.status === "fulfilled") applySnapshot(snap.value);
        if (currentPlan.status === "fulfilled") guard(() => setPlan(currentPlan.value));
        if (activity.status === "fulfilled") applyTools(activity.value);
        if (weak.status === "fulfilled") guard(() => setWeaknesses(weak.value.weaknesses));
        if (lead.status === "fulfilled") guard(() => setLeads(lead.value.leads));
        if (cost.status === "fulfilled") guard(() => setUsage(cost.value));
        if (chat.status === "fulfilled") applyConversation(chat.value);
        if (eventPage.status === "fulfilled") {
          const page = eventPage.value;
          guard(() => setEvents((previous) => mergeEvents(previous, page.events)));
        }
        if (snap.status === "fulfilled") {
          guard(() => setFindings(snap.value.findings));
        }
      } finally {
        refreshing = false;
        if (refreshRequested) scheduleRefresh();
      }
    };
    refreshRef.current = () => refresh();

    const scheduleRefresh = () => {
      refreshRequested = true;
      if (refreshTimer !== undefined || refreshing || closed) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        refreshRequested = false;
        void refresh();
      }, 350);
    };

    const start = async () => {
      source?.close();
      source = null;
      setConnection("connecting");
      try {
        const [snap, eventPage, chat] = await Promise.all([
          api.snapshot(session),
          api.eventPage(session),
          api.conversation(session),
        ]);
        if (closed || generation !== genRef.current) return;
        lastSeq = eventPage.events.at(-1)?.session_seq ?? 0;
        applySnapshot(snap);
        guard(() => {
          setEvents(previous => mergeEvents(previous, eventPage.events));
          setConversation(previous => mergeConversation(previous, chat.entries));
        });
        if (!loaded) {
          conversationNextRef.current = chat.next_cursor;
          setHasMoreMessages(!!chat.next_cursor);
          loaded = true;
        }

        const current = latestRun(snap);
        const draft = current && ["running", "waiting_tool", "pausing", "paused", "cancelling"].includes(current.status)
          ? await restoreAssistantDraft(current, eventPage, cursor => api.eventPage(session, cursor), () => closed)
          : null;
        if (closed || generation !== genRef.current) return;
        setAssistantDraft(draft);

        lastEventAt = Date.now();
        const connection = new EventSource(`/api/sessions/${session}/events?after=${lastSeq}`);
        source = connection;
        scheduleRefresh();
        source.onopen = () => { if (source === connection) guard(() => setConnection("online")); };
        source.onerror = () => { if (source === connection) guard(() => setConnection("offline")); };
        for (const type of EVENT_TYPES) {
          source.addEventListener(type, (raw) => {
            if (closed || generation !== genRef.current || source !== connection) return;
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
            if (event.session_seq !== lastSeq + 1) {
              recover();
              return;
            }
            lastSeq = event.session_seq;
            lastEventAt = Date.now();
            guard(() => setEvents((previous) => mergeEvents(previous, [event])));
            const delta = assistantDelta(event);
            if (delta) setAssistantDraft(previous => appendAssistantDelta(previous, delta));
            else if (event.type !== "message.delta" || event.payload?.event === "final_response_saved") scheduleRefresh();
          });
        }
      } catch (cause) {
        if (closed || generation !== genRef.current) return;
        setConnection("offline");
        setNotice(`无法读取任务：${String(cause)}`);
      }
    };

    const recover = () => {
      if (closed || recoveryPending) return;
      recoveryPending = true;
      void start().finally(() => {
        recoveryPending = false;
      });
    };

    recover();
    watchdog = window.setInterval(() => {
      if (closed || recoveryPending) return;
      void api
        .snapshot(session)
        .then((value) => {
          if (closed || generation !== genRef.current) return;
          if (!source || source.readyState === EventSource.CLOSED || (value.event_cursor > lastSeq && Date.now() - lastEventAt > 3000)) {
            recover();
          } else {
            applySnapshot(value);
          }
        })
        .catch(() => guard(() => setConnection("offline")));
    }, 3000);

    return () => {
      closed = true;
      genRef.current += 1;
      source?.close();
      if (refreshRef.current) refreshRef.current = null;
      if (refreshTimer) window.clearTimeout(refreshTimer);
      if (watchdog) window.clearInterval(watchdog);
    };
  }, [ready, sessionId, setNotice]);

  /* ---------------- 执行端真实状态 ---------------- */
  useEffect(() => {
    if (!sessionId) {
      setExecutorState("unknown");
      setExecutorId(null);
      return;
    }
    let active = true;
    setExecutorState("checking");
    void api
      .executionEnvironment(sessionId)
      .then((report) => {
        if (!active) return;
        setExecutorState("online");
        setExecutorId(report.executor_id);
      })
      .catch(() => {
        if (!active) return;
        setExecutorState("offline");
        setExecutorId(null);
      });
    return () => {
      active = false;
    };
  }, [sessionId]);

  /* ---------------- 动作 ---------------- */
  const selectSession = useCallback(
    (id: string) => {
      if (!id) return;
      const projectId = sessionKey(projectGroups, id);
      setSessionId(id);
      setSelectedProjectId((previous) => projectId || previous);
      if (projectId) void ensureProjectSessions(projectId);
      setPendingProjectId(null);
      setNoticeState("");
      window.history.replaceState(null, "", `?session=${encodeURIComponent(id)}`);
    },
    [ensureProjectSessions, projectGroups],
  );

  const newChat = useCallback(
    (projectId?: string | null) => {
      preferredSessionRef.current = "";
      setExecutionMode("ultra");
      setSessionId("");
      setPendingProjectId(projectId ?? null);
      setSelectedProjectId(projectId ?? "");
      setNoticeState("");
      window.history.replaceState(null, "", window.location.pathname);
    },
    [],
  );

  const refreshProject = useCallback(async (projectId: string | null) => {
    if (!projectId) { setStandalone(await api.allSessions(null)); return; }
    const page = { sessions: await api.allSessions(projectId) };
    loadedProjectsRef.current.add(projectId);
    setProjects((previous) =>
      previous.map((project) => (project.id === projectId ? { ...project, sessions: page.sessions } : project)),
    );
  }, []);

  const refreshProjects = useCallback(async () => {
    const data = await api.projects();
    setStandalone(await api.allSessions(null));
    setProjects((previous) => {
      const sessions = new Map(previous.map((project) => [project.id, project.sessions]));
      return data.projects.map((project) => ({ ...project, sessions: sessions.get(project.id) ?? [] }));
    });
  }, []);

  const reconnect = useCallback(async () => {
    setConnection("connecting");
    try {
      setAttachmentsEnabled(await api.health());
      await api.signIn();
      await refreshProjects();
      setBootError("");
      setConnection("online");
      setReady(true);
    } catch (cause) {
      setBootError(String(cause instanceof Error ? cause.message : cause));
      setConnection("offline");
      setReady(false);
      throw cause;
    }
  }, [refreshProjects]);

  const ensureProjectId = useCallback(async () => pendingProjectId, [pendingProjectId]);

  const createProject = useCallback(async (name: string, description: string) => {
    const created = await api.createProject({
      name: name.trim(),
      target_description: description.trim() || name.trim(),
      settings_version: "1",
    });
    setProjects((previous) => [...previous, { ...created, sessions: [] }]);
    setSelectedProjectId(created.id);
    return created;
  }, []);

  const prepareAttachmentSession = useCallback(async () => {
    if (sessionId) return sessionId;
    const projectId = await ensureProjectId();
    const profile = modelSettings?.profiles.find((item) => `${item.provider_id}/${item.model_name}` === selectedModelKey);
    const created = await api.createSession(projectId, {
      execution_mode: "ultra",
      project_id: projectId, title: "附件草稿", initial_objective: "", draft: true,
      provider_id: profile?.provider_id, model_name: profile?.model_name,
    });
    await refreshProject(projectId);
    return created.session.id;
  }, [ensureProjectId, executionMode, modelSettings, refreshProject, selectedModelKey, sessionId]);

  const send = useCallback(
    async (text: string, attachments?: string[], attachmentSession?: string, messageKey?: string, skillRefs?: string[]) => {
      const content = text.trim();
      if (!content || busy) return false;
      setBusy(true);
      setNoticeState("");
      try {
        const targetSession = attachmentSession || sessionId;
        if (targetSession) {
          if (modelLocked) {
            // 活动 run 固定模型；后端也不接受中途换模型。
            await api.message(targetSession, content, undefined, skillRefs, attachments, messageKey);
          } else {
            const chosen = modelSettings?.profiles.find(
              (item) => `${item.provider_id}/${item.model_name}` === selectedModelKey,
            );
            if (!chosen || !chosen.secret_configured) {
              setNoticeState("所选模型尚未配置 Key。请先在任务详情 → 设置完成配置，或在输入框切换模型。");
              return false;
            }
            await api.message(targetSession, content, {
              provider_id: chosen.provider_id,
              model_name: chosen.model_name,
            }, skillRefs, attachments, messageKey);
          }
          if (!sessionId) {
            setSessionId(targetSession);
            window.history.replaceState(null, "", `?session=${encodeURIComponent(targetSession)}`);
          }
          window.setTimeout(() => void refreshRef.current?.(), 500);
          return true;
        }
        const profile =
          modelSettings?.profiles.find(
            (item) => `${item.provider_id}/${item.model_name}` === selectedModelKey,
          ) ?? modelSettings?.profiles.find(
            (item) =>
              item.provider_id === modelSettings.default_provider && item.model_name === modelSettings.default_model,
          ) ?? modelSettings?.profiles.find((item) => item.secret_configured);
        if (!profile || !profile.secret_configured) {
          setNoticeState("尚未配置模型 Key。请先打开任务详情 → 设置完成配置，再发送任务。");
          return false;
        }
        const projectId = await ensureProjectId();
        const created = await api.createSession(projectId, {
          execution_mode: "ultra",
          project_id: projectId,
          title: content.slice(0, 30) || "新测试任务",
          initial_objective: content,
          draft: false,
          provider_id: profile.provider_id,
          model_name: profile.model_name,
          scope: { mode: "instruction", revision: 1, rules: [] },
          initial_skill_refs: skillRefs ?? [],
        });
        await refreshProject(projectId);
        setSessionId(created.session.id);
        setSelectedProjectId(projectId ?? "");
        setPendingProjectId(null);
        window.history.replaceState(null, "", `?session=${encodeURIComponent(created.session.id)}`);
        return true;
      } catch (cause) {
        setNoticeState(String(cause instanceof Error ? cause.message : cause));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [busy, ensureProjectId, executionMode, modelLocked, modelSettings, refreshProject, selectedModelKey, sessionId],
  );

  const control = useCallback(
    async (action: "pause" | "resume" | "cancel", acknowledge = false) => {
      const current = latestRun(snapshot);
      if (!current || busy) return false;
      setBusy(true);
      setNoticeState("");
      try {
        await api.control(current.id, action, acknowledge);
        setNoticeState(
          action === "pause"
            ? "暂停指令已提交；仍需等待执行端确认。"
            : action === "resume"
              ? "恢复指令已提交；接下来以真实事件为准。"
              : "取消指令已提交；仍需等待执行状态确认。",
        );
        window.setTimeout(() => void refreshRef.current?.(), 400);
        return true;
      } catch (cause) {
        if (
          action === "resume" &&
          cause instanceof ApiRequestError &&
          cause.status === 409 &&
          cause.details?.required_field === "acknowledge_unknown_operations"
        ) {
          setAckRun(current);
          return false;
        }
        setNoticeState(String(cause instanceof Error ? cause.message : cause));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [busy, snapshot],
  );

  const resumeWithAcknowledgement = useCallback(async () => {
    const ok = await control("resume", true);
    if (ok) setAckRun(null);
    return ok;
  }, [control]);

  const loadOlderMessages = useCallback(async () => {
    const cursor = conversationNextRef.current;
    if (!sessionId || !cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.conversation(sessionId, cursor);
      setConversation((previous) => mergeConversation(previous, page.entries));
      conversationNextRef.current = page.next_cursor;
      setHasMoreMessages(!!page.next_cursor);
    } catch (cause) {
      setNoticeState(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, sessionId]);

  const loadMoreTools = useCallback(async () => {
    const offset = toolNextRef.current;
    if (!sessionId || offset === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.toolActivities(sessionId, offset);
      setTools((previous) => mergeTools(previous, page.activities));
      toolNextRef.current = page.next_offset;
      setHasMoreTools(page.next_offset !== null);
    } catch (cause) {
      setNoticeState(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, sessionId]);

  const deleteSession = useCallback(
    async (target: SessionItem, terminateResources = false): Promise<DeleteResult> => {
      try {
        const result = await api.deleteSession(target.id, terminateResources);
        if (!result.deleted) return { ok: false, message: "停止仍在确认，请稍后再删除。" };
        await refreshProject(target.project_id);
        if (sessionId === target.id) {
          setSessionId("");
          window.history.replaceState(null, "", window.location.pathname);
        }
        return { ok: true, pending: result.cancellation_pending };
      } catch (cause) {
        if (cause instanceof ApiRequestError && cause.status === 409) {
          const resources = Array.isArray(cause.details?.active_resources)
            ? (cause.details.active_resources as string[])
            : [];
          return { ok: false, blocked: true, message: cause.message, resources };
        }
        return { ok: false, message: String(cause instanceof Error ? cause.message : cause) };
      }
    },
    [refreshProject, sessionId],
  );

  const refresh = useCallback(async () => {
    await refreshRef.current?.();
  }, []);

  return {
    ready,
    attachmentsEnabled,
    bootError,
    connection,
    projects: projectGroups,
    standalone,
    recent,
    sessionId,
    selectedProjectId,
    pendingProjectId,
    defaultProjectId,
    session,
    snapshot,
    run,
    conversation,
    tools,
    plan,
    weaknesses,
    findings,
    leads,
    usage,
    events,
    inputRequired,
    assistantDraft,
    assistantModelCallId,
    lastActivityAt,
    modelPhase,
    hasMoreMessages,
    hasMoreTools,
    loadingMore,
    busy,
    notice,
    modelSettings,
    modelOptions,
    executionMode,
    selectedModelKey,
    currentModelKey,
    modelLocked,
    setSelectedModelKey,
    skillCount,
    executorState,
    executorId,
    ackRun,
    selectSession,
    newChat,
    setSelectedProjectId,
    setPendingProjectId,
    setNotice,
    refresh,
    refreshProjects,
    refreshProject,
    reconnect,
    ensureProjectSessions,
    isProjectLoaded: (projectId: string) => loadedProjectsRef.current.has(projectId),
    createProject,
    send,
    prepareAttachmentSession,
    control,
    clearAck: () => setAckRun(null),
    resumeWithAcknowledgement,
    loadOlderMessages,
    loadMoreTools,
    deleteSession,
    reloadModels: () => setModelEpoch((value) => value + 1),
    onModelsSaved: (settings) => {
      if (settings.default_provider !== modelSettings?.default_provider || settings.default_model !== modelSettings?.default_model) {
        setSelectedModelKey(settings.default_provider ? `${settings.default_provider}/${settings.default_model}` : "");
      }
      setModelSettings(settings);
      setModelEpoch((value) => value + 1);
    },
  };
}
