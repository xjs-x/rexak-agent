import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AgentEvent,
  type ModelCallItem,
  type ModelSettings,
  type OperationItem,
  type ReportItem,
  type Snapshot,
  type SpanItem,
  type UsageSummary,
} from "../api/client";
import { ExecutionPanel } from "../ExecutionPanel";
import { ModelConfiguration } from "../ModelConfiguration";
import { SkillLibrary } from "../SkillLibrary";
import { Icon } from "./Icon";
import {
  durationLabel,
  formatTime,
  modeLabel,
  runLabel,
  textOf,
} from "./labels";

export type DrawerTab = "activity" | "resources" | "cost" | "diagnostics" | "reports" | "settings" | "skills";

const TABS: Array<[DrawerTab, string]> = [
  ["activity", "实时活动"],
  ["resources", "执行资源"],
  ["cost", "费用"],
  ["diagnostics", "诊断"],
  ["reports", "报告"],
  ["settings", "设置"],
  ["skills", "技能库"],
];

function summarizeEvent(event: AgentEvent): string {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  if (event.type === "message.delta") return textOf(payload.delta ?? "", 140);
  if (event.type === "run.status") {
    if (payload.scope === "model_request") return `模型请求 · ${String(payload.phase ?? "")}`;
    if (payload.event === "scope_updated") return `范围更新 · v${String(payload.scope_revision ?? "")}`;
    return `运行状态：${runLabel(String(payload.status ?? ""))}`;
  }
  if (event.type === "input.required") return textOf(payload.message ?? payload.reason ?? "", 160);
  if (event.type === "tool.completed" || event.type === "tool.accepted" || event.type === "tool.output") {
    return `${String(payload.tool_name ?? "工具")} · ${String(payload.status ?? payload.event ?? "")}`;
  }
  if (event.type === "weakness.changed") return textOf(payload.title ?? payload.weakness_id ?? "", 140);
  if (event.type === "report.ready") return `报告 v${String(payload.version ?? "")} 已生成`;
  return textOf(payload, 160);
}

export function TaskDrawer({
  open,
  tab,
  onTab,
  onClose,
  sessionId,
  projectId,
  sessionTitle,
  snapshot,
  modelSettings,
  onModelsSaved,
  onOpenReport,
  refreshToken,
  onToast,
}: {
  open: boolean;
  tab: DrawerTab;
  onTab: (tab: DrawerTab) => void;
  onClose: () => void;
  sessionId: string;
  projectId: string;
  sessionTitle: string;
  snapshot: Snapshot | null;
  modelSettings: ModelSettings | null;
  onModelsSaved: (settings: ModelSettings) => void;
  onOpenReport: (version: number) => void;
  refreshToken: number;
  onToast: (message: string) => void;
}) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [before, setBefore] = useState<number | null>(null);
  const [eventBusy, setEventBusy] = useState(false);
  const [eventError, setEventError] = useState("");
  const [eventDetail, setEventDetail] = useState<AgentEvent | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [calls, setCalls] = useState<ModelCallItem[]>([]);
  const [spans, setSpans] = useState<SpanItem[]>([]);
  const [operations, setOperations] = useState<OperationItem[]>([]);
  const [reports, setReports] = useState<ReportItem[]>([]);
  const [reportBusy, setReportBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [executorNote, setExecutorNote] = useState("");
  const [executorOnline, setExecutorOnline] = useState<boolean | null>(null);
  const [priceNote, setPriceNote] = useState("");

  const loadEvents = useCallback(
    async (older = false) => {
      if (!sessionId || eventBusy) return;
      setEventBusy(true);
      setEventError("");
      try {
        const page = await api.eventPage(sessionId, older ? (before ?? undefined) : undefined);
        setEvents((previous) => {
          if (!older) return page.events;
          const map = new Map(previous.map((event) => [event.session_seq, event]));
          for (const event of page.events) map.set(event.session_seq, event);
          return [...map.values()].sort((left, right) => right.session_seq - left.session_seq);
        });
        setBefore(page.next_before);
      } catch (cause) {
        setEventError(String(cause instanceof Error ? cause.message : cause));
      } finally {
        setEventBusy(false);
      }
    },
    [before, eventBusy, sessionId],
  );

  useEffect(() => {
    if (!open || tab !== "activity" || !sessionId) return;
    setEvents([]);
    setBefore(null);
    setEventDetail(null);
    void loadEvents(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tab, sessionId, refreshToken]);

  useEffect(() => {
    if (!open || !sessionId) return;
    let active = true;
    setError("");
    setLoading(false);
    if (tab === "cost") {
      setLoading(true);
      void Promise.all([api.usage(sessionId), api.modelCalls(sessionId)])
        .then(([cost, page]) => {
          if (!active) return;
          setUsage(cost);
          setCalls(page.model_calls);
        })
        .catch((cause) => active && setError(String(cause)))
        .finally(() => active && setLoading(false));
    }
    if (tab === "diagnostics") {
      setLoading(true);
      void Promise.all([api.spans(sessionId), api.operations(sessionId)])
        .then(([spanPage, operationPage]) => {
          if (!active) return;
          setSpans(spanPage.spans);
          setOperations(operationPage.operations);
        })
        .catch((cause) => active && setError(String(cause)))
        .finally(() => active && setLoading(false));
    }
    if (tab === "reports") {
      setLoading(true);
      void api.reports(sessionId)
        .then((page) => active && setReports(page.reports))
        .catch((cause) => active && setError(String(cause)))
        .finally(() => active && setLoading(false));
    }
    if (tab === "settings") {
      setLoading(true);
      void api.executionEnvironment(sessionId)
        .then((report) => {
          if (!active) return;
          setExecutorOnline(true);
          setExecutorNote(`${report.executor_id} · ${report.platform} · ${report.os_release}`);
        })
        .catch((cause) => {
          if (!active) return;
          setExecutorOnline(false);
          setExecutorNote(String(cause instanceof Error ? cause.message : cause));
        })
        .finally(() => active && setLoading(false));
      void api.priceCatalog()
        .then((catalog) => active && setPriceNote(`价格目录 v${catalog.version} · ${catalog.prices.length} 项；未配置价格的模型保持“未定价”。`))
        .catch(() => active && setPriceNote("价格目录暂时无法读取。"));
    }
    return () => {
      active = false;
    };
  }, [open, tab, sessionId, refreshToken]);

  const createReport = async () => {
    if (!sessionId || reportBusy) return;
    setReportBusy(true);
    setError("");
    try {
      const report = await api.createReport(sessionId);
      setReports((previous) => [report, ...previous.filter((item) => item.version !== report.version)]);
      onToast(`报告 v${report.version} 已生成`);
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setReportBusy(false);
    }
  };

  if (!open) return null;

  const scope = snapshot?.session.scope;
  const recentCalls = [...calls].sort(
    (left, right) => Date.parse(right.submitted_at ?? "") - Date.parse(left.submitted_at ?? ""),
  );

  return (
    <aside className="drawer open" aria-label="任务详情">
      <div className="drawer-head">
        <h2>任务详情</h2>
        <span className="sub">{sessionTitle || "未选择任务"}</span>
        <button className="icon-btn close" title="关闭" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className="drawer-tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? "active" : ""} onClick={() => onTab(key)}>
            {label}
          </button>
        ))}
      </div>
      <div className="drawer-body">
        {!sessionId && <div className="drawer-empty">先选择一个任务，再从真实接口读取详情。</div>}
        {sessionId && error && <div className="drawer-error">{error}</div>}

        {sessionId && tab === "activity" && (
          <>
            <p className="drawer-sub">按任务事件顺序读取；点击一行可查看服务端脱敏后的事件内容。</p>
            <div className="drawer-actions">
              <button className="mini-btn" disabled={eventBusy} onClick={() => void loadEvents(false)}>
                {eventBusy ? "读取中…" : "刷新"}
              </button>
              {before !== null && (
                <button className="mini-btn" disabled={eventBusy} onClick={() => void loadEvents(true)}>
                  更早一页
                </button>
              )}
            </div>
            {eventError && <div className="drawer-error">{eventError}</div>}
            {events.length === 0 && !eventBusy && <div className="drawer-empty">还没有事件记录。</div>}
            {events.map((event) => (
              <div key={event.event_id}>
                <button
                  className="ev"
                  style={{ width: "100%", textAlign: "left" }}
                  onClick={() =>
                    void api
                      .eventAt(sessionId, event.session_seq)
                      .then(setEventDetail)
                      .catch((cause) => setEventError(String(cause)))
                  }
                >
                  <span className="seq">#{event.session_seq}</span>
                  <div>
                    <div className="type">{event.type}</div>
                    <div className="sum">{summarizeEvent(event)}</div>
                  </div>
                  <span className="tm">{formatTime(event.occurred_at).slice(-8)}</span>
                </button>
                {eventDetail?.event_id === event.event_id && (
                  <pre className="artifact-pre" style={{ maxHeight: 220, margin: "0 0 8px 56px" }}>
                    {JSON.stringify(eventDetail.payload, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </>
        )}

        {sessionId && tab === "resources" && (
          <>
            <p className="drawer-sub">资源状态来自执行端；取消值班不会关闭资源，关闭资源也不会改写历史记录。</p>
            <ExecutionPanel sessionId={sessionId} />
          </>
        )}

        {sessionId && tab === "cost" && (
          <>
            {loading && <div className="drawer-loading">读取费用与用量…</div>}
            {usage && (
              <>
                <div className="tok">
                  <div><small>输入 tokens</small><strong>{usage.token_totals.input_tokens ?? "—"}</strong></div>
                  <div><small>输出 tokens</small><strong>{usage.token_totals.output_tokens ?? "—"}</strong></div>
                  <div><small>缓存读取</small><strong>{usage.token_totals.cache_read_tokens ?? "—"}</strong></div>
                  <div><small>推理 tokens</small><strong>{usage.token_totals.reasoning_tokens ?? "—"}</strong></div>
                </div>
                <div className="d-sec">
                  <div className="d-row"><span className="k">模型请求</span><span className="v">{usage.call_count}</span></div>
                  {Object.entries(usage.by_currency).map(([currency, value]) => (
                    <div className="d-row" key={currency}>
                      <span className="k">已知费用</span>
                      <span className="v">{value.known_cost} {currency} · {value.priced_calls} 次计价</span>
                    </div>
                  ))}
                  <div className="d-row"><span className="k">未定价请求</span><span className="v">{usage.unpriced_calls}</span></div>
                  <div className="d-row"><span className="k">用量未知</span><span className="v">{usage.unknown_usage_calls}</span></div>
                </div>
                <p className="fine">{usage.note}</p>
              </>
            )}
            <h4 style={{ marginTop: 16 }}>模型调用</h4>
            {recentCalls.length === 0 && !loading && <div className="drawer-empty">暂无模型调用记录。</div>}
            {recentCalls.map((call) => (
              <div className="d-row" key={call.model_call_id}>
                <span className="k">{call.phase}</span>
                <span className="v">
                  {call.model_name} · in {call.input_tokens ?? "—"} / out {call.output_tokens ?? "—"}
                  {call.cost_amount ? ` · ${call.cost_amount} ${call.cost_currency ?? ""}` : " · 未定价"}
                  {call.error_code ? ` · ${call.error_code}` : ""}
                </span>
                <span className="r">{formatTime(call.submitted_at).slice(-8)}</span>
              </div>
            ))}
          </>
        )}

        {sessionId && tab === "diagnostics" && (
          <>
            {loading && <div className="drawer-loading">读取诊断数据…</div>}
            <h4>耗时诊断</h4>
            {spans.length === 0 && !loading && <div className="drawer-empty">暂无诊断 span。</div>}
            {spans.map((span) => (
              <div className="d-row" key={span.id}>
                <span className="k" style={{ width: 170, flexBasis: 170, fontFamily: "var(--mono)", fontSize: 10.5 }}>{span.name}</span>
                <span className="v">{span.decision || span.status || "—"}{span.error ? ` · ${span.error}` : ""}</span>
                <span className="r">{span.duration_ms !== null ? `${Math.round(span.duration_ms)} 毫秒` : "—"}</span>
              </div>
            ))}
            <h4 style={{ marginTop: 16 }}>工具操作</h4>
            {operations.length === 0 && !loading && <div className="drawer-empty">暂无工具操作记录。</div>}
            {operations.map((operation) => (
              <div className="d-row" key={operation.operation_id}>
                <span className="k">{operation.tool_name}</span>
                <span className="v">
                  {operation.status}
                  {operation.job_state ? ` · job ${operation.job_state}` : ""}
                  {operation.sync_status && operation.sync_status !== "in_sync" ? " · 同步待核对" : ""}
                </span>
                <span className="r">{durationLabel(operation.executor_started_at, operation.executor_finished_at) || formatTime(operation.created_at).slice(-8)}</span>
              </div>
            ))}
          </>
        )}

        {sessionId && tab === "reports" && (
          <>
            <div className="drawer-actions">
              <button className="mini-btn strong" disabled={reportBusy} onClick={() => void createReport()}>
                {reportBusy ? "生成中…" : "生成新版本"}
              </button>
            </div>
            <p className="drawer-sub">每次生成保留历史；报告正文在工件里，导出链接由后端直接提供。</p>
            {loading && <div className="drawer-loading">读取报告版本…</div>}
            {reports.length === 0 && !loading && <div className="drawer-empty">还没有报告版本。</div>}
            {reports.map((report) => (
              <div className="report" key={report.version}>
                <span className="rn">v{report.version}</span>
                <div>
                  <strong>{report.status}{report.is_historical ? " · 历史版本" : ""}</strong>
                  <small>{formatTime(report.created_at)}</small>
                </div>
                <button className="btn" onClick={() => onOpenReport(report.version)}>查看</button>
              </div>
            ))}
          </>
        )}

        {sessionId && tab === "settings" && (
          <>
            <h4>当前任务范围</h4>
            <p className="drawer-sub">范围来自 session 快照；本页只读，变更需要走后端范围接口。</p>
            <div className="d-row"><span className="k">模式</span><span className="v">{modeLabel(scope?.mode)} · v{scope?.revision ?? "—"}</span></div>
            {(scope?.rules ?? []).map((rule, index) => (
              <div className="scope-rule" key={`${rule.host}-${index}`}>
                {rule.scheme}://{rule.host}{rule.port ? `:${rule.port}` : ""}
                {rule.include_subdomains ? " · 含子域" : ""}
              </div>
            ))}
            {(scope?.rules ?? []).length === 0 && <p className="fine">按指令运行：目标由用户消息解析。</p>}

            <h4 style={{ marginTop: 18 }}>执行端</h4>
            <div className="d-row">
              <span className="k">状态</span>
              <span className="v">{executorOnline === null ? "查询中…" : executorOnline ? "在线" : "不可达"}</span>
            </div>
            <p className="fine">{executorNote || "执行端信息来自 execution/environment 接口。"}</p>

            <h4 style={{ marginTop: 18 }}>模型</h4>
            <p className="fine">{priceNote || "价格目录读取中…"}</p>
            <ModelConfiguration settings={modelSettings} onSaved={onModelsSaved} />
          </>
        )}

        {sessionId && tab === "skills" && (
          <>
            <p className="drawer-sub">
              技能库用于导入、启停和查看版本。当前后端在消息接口里拒绝 skill_refs，因此这里的选择不会随消息注入。
            </p>
            <SkillLibrary
              sessionId={sessionId}
              projectId={projectId}
              selected={[]}
              onSelected={() => onToast("当前阶段后端暂不启用技能注入；技能只用于查看与管理。")}
              note="当前阶段后端拒绝 skill_refs：这里只用于导入、启停和查看版本，不会随消息注入。"
            />
          </>
        )}
      </div>
    </aside>
  );
}
