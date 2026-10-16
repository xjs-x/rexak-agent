import { useEffect, useState } from "react";
import { api, ApiRequestError } from "./api/client";
import type { components } from "./api/generated/schema";

type ResourceCard = components["schemas"]["ExecutionResourceCard"];
type DutyCard = components["schemas"]["DutyCard"];
type ResourcesResponse = components["schemas"]["ExecutionResourcesResponse"];
type OutputPage = components["schemas"]["ResourceOutputResponse"];

const stateLabels: Record<string, string> = {
  starting: "正在启动",
  running: "正在运行",
  exited: "已退出",
  closed: "已关闭",
  unknown: "状态待核对",
};
const interactionLabels: Record<string, string> = {
  available: "可交互",
  lost: "交互已丢失",
  not_applicable: "不适用（非交互）",
};
const terminationLabels: Record<string, string> = {
  exited_normally: "自行退出",
  execution_timeout: "执行超时终止",
  user_cancelled: "取消终止",
  output_limit_terminate: "日志超限终止",
  failure: "故障终止",
  unknown: "原因未知",
};

/** 用途独立于进程类型；命令启动的监听器也可以作为保留资源。 */
function isPendingWork(item: ResourceCard): boolean {
  return (
    item.detail?.completion_role !== "resource" &&
    ["command", "script"].includes(item.kind) &&
    ["starting", "running", "unknown"].includes(item.state)
  );
}

function isRetained(item: ResourceCard): boolean {
  return ["starting", "running", "unknown"].includes(item.state);
}

function ResourceDetail({
  sessionId,
  resource,
}: {
  sessionId: string;
  resource: ResourceCard;
}) {
  const [page, setPage] = useState<OutputPage | null>(null);
  const [view, setView] = useState<"stream" | "screen">(
    resource.kind === "terminal" ? "screen" : "stream",
  );
  const [error, setError] = useState("");
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const [loading, setLoading] = useState(false);
  const load = async (cursor: string | null = null) => {
    setLoading(true);
    setError("");
    try {
      setPage(
        await api.executionResourceOutput(sessionId, resource.resource_id, {
          view,
          cursor,
          stream,
        }),
      );
    } catch (cause) {
      setError(String(cause));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void load();
    // 只在资源/视图变化时重新读取；轮询由上层负责。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resource.resource_id, view, resource.state, stream]);
  return (
    <div className="resource-detail">
      <div className="resource-view-toggle">
        <button className={view === "stream" ? "active" : ""} onClick={() => setView("stream")}>
          输出流
        </button>
        {resource.kind === "terminal" && (
          <button className={view === "screen" ? "active" : ""} onClick={() => setView("screen")}>
            屏幕快照
          </button>
        )}
        {resource.kind !== "terminal" && <>
          <button className={stream === "stdout" ? "active" : ""} onClick={() => setStream("stdout")}>标准输出</button>
          <button className={stream === "stderr" ? "active" : ""} onClick={() => setStream("stderr")}>错误输出</button>
        </>}
        <button onClick={() => void load()} disabled={loading}>
          {loading ? "读取中…" : "刷新"}
        </button>
      </div>
      {error && <p className="body-error">读取失败：{error}</p>}
      {page && (
        <>
          <pre className="resource-output">{page.text || "（当前没有新的输出）"}</pre>
          {view === "stream" && page.next_cursor && !page.at_current_end && (
            <button onClick={() => void load(page.next_cursor ?? null)} disabled={loading}>读取下一页</button>
          )}
          {view === "stream" && <a href={`/api/sessions/${sessionId}/execution/resources/${resource.resource_id}/raw-output?stream=${stream}&offset=0`} download>下载原始日志前 1 MiB 快照</a>}
          <p className="tool-note">
            {page.truncated ? "⚠ 采集日志超限，存在缺失区间。" : ""}
            {page.missing_ranges && page.missing_ranges.length > 0
              ? `缺失区间：${page.missing_ranges.map((item) => `${item.start}-${item.end}`).join("、")}；`
              : ""}
            {page.encoding_error ? `编码问题：${page.encoding_error}；` : ""}
            {page.at_current_end
              ? "已读到当前末尾（不代表程序已结束）。"
              : "还有更多输出，可用游标继续读取。"}
            {page.note ? ` ${page.note}` : ""}
          </p>
        </>
      )}
    </div>
  );
}

function ResourceEntry({ sessionId, resource, onChanged }: {
  sessionId: string;
  resource: ResourceCard;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [confirmClose, setConfirmClose] = useState(false);
  const close = async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await api.closeExecutionResource(sessionId, resource.resource_id);
      setMessage(
        result.process_confirmed_exited
          ? "已确认进程退出。"
          : "关闭请求已发出，但**尚未确认进程退出**（可能仍在运行）。",
      );
      setConfirmClose(false);
      onChanged();
    } catch (cause) {
      setMessage(String(cause));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`resource-card state-${resource.state}`}>
      <div className="resource-head">
        <span className="resource-kind">{resource.kind}</span>
        <span className="resource-state">{stateLabels[resource.state] ?? resource.state}</span>
        <span className="resource-interaction">
          交互：{interactionLabels[resource.interaction_state] ?? resource.interaction_state}
        </span>
        <button onClick={() => setOpen(!open)}>{open ? "收起" : "详情"}</button>
      </div>
      <div className="resource-meta">
        {resource.purpose || "（未说明用途）"}
        {resource.mode ? ` · 模式 ${resource.mode}` : ""}
        {resource.exit_code !== null && resource.exit_code !== undefined
          ? ` · 退出码 ${resource.exit_code}`
          : ""}
        {resource.termination_reason
          ? ` · ${terminationLabels[resource.termination_reason] ?? resource.termination_reason}`
          : ""}
        {resource.last_checked_at
          ? ` · 最近核对 ${new Date(resource.last_checked_at).toLocaleString("zh-CN", { hour12: false })}`
          : ""}
      </div>
      <div className="resource-meta">
        配额：{String(resource.quota?.output_bytes ?? "未知")} / {String(resource.quota?.output_limit_bytes ?? "未知")} 字节
        {Number(resource.quota?.dropped_bytes ?? 0) > 0
          ? `（超限丢弃 ${String(resource.quota?.dropped_bytes)} 字节，缺失区间在输出里标注）`
          : ""}
        {resource.close_state !== "open"
          ? ` · 关闭状态：${resource.close_confirmed ? "已确认" : "未确认"}`
          : ""}
        {resource.sync_status !== "in_sync" ? " · 状态同步待核对" : ""}
      </div>
      <div className="resource-actions">
        {confirmClose ? (
          <>
            <span className="resource-warn">关闭资源会终止该进程；取消未确认时会如实显示。</span>
            <button className="danger" disabled={busy} onClick={() => void close()}>
              确认关闭
            </button>
            <button disabled={busy} onClick={() => setConfirmClose(false)}>
              保留
            </button>
          </>
        ) : (
          <button disabled={busy} onClick={() => setConfirmClose(true)}>
            关闭资源
          </button>
        )}
      </div>
      {message && <p className="tool-note">{message}</p>}
      {open && <ResourceDetail sessionId={sessionId} resource={resource} />}
    </div>
  );
}

/** 执行资源面板：本阶段结果 / 待完成工作 / 保留资源分开显示，值班与资源状态分开。 */
export function ExecutionPanel({ sessionId }: { sessionId: string }) {
  const [data, setData] = useState<ResourcesResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [quote, setQuote] = useState("");
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<"once" | "continuous">("once");
  const [targetIds, setTargetIds] = useState("");

  const refresh = async (refreshRemote = true) => {
    try {
      setData(await api.executionResources(sessionId, { refresh: refreshRemote }));
      setError("");
    } catch (cause) {
      setError(String(cause));
    }
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(false), 8000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const pending = (data?.resources ?? []).filter(isPendingWork);
  const retained = (data?.resources ?? []).filter(
    (item) => isRetained(item) && !isPendingWork(item),
  );
  const finished = (data?.resources ?? []).filter(
    (item) => !isRetained(item) && !isPendingWork(item),
  );
  const activeDuties = (data?.duties ?? []).filter((item) => item.status === "active");

  const terminateAll = async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await api.terminateAllExecutionResources(sessionId);
      setMessage(
        result.unresolved.length > 0
          ? `已发出终止请求；${result.unresolved.length} 个资源尚未确认关闭，请核对列表。`
          : "所有资源均已确认关闭。",
      );
      await refresh();
    } catch (cause) {
      setMessage(String(cause));
    } finally {
      setBusy(false);
    }
  };

  const createDuty = async () => {
    setBusy(true);
    setMessage("");
    try {
      const created = await api.createDuty(sessionId, {
        resource_ids: targetIds
          .split(/[\s,]+/)
          .map((item) => item.trim())
          .filter(Boolean),
        events: ["resource.exited", "resource.watch_match", "resource.interaction_lost", "output.limit_reached"],
        goal,
        mode,
        authorization_quote: quote,
      });
      setMessage(`委托已建立（${created.status}，revision ${created.revision ?? "-"}）。`);
      setQuote("");
      await refresh();
    } catch (cause) {
      // 403 表示引用片段不在用户消息里：这是确定性核对，不是网络故障。
      setMessage(cause instanceof ApiRequestError ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const cancelDuty = async (delegationId: string) => {
    setBusy(true);
    setMessage("");
    try {
      const result = await api.cancelDuty(sessionId, delegationId, "UI 取消值班");
      setMessage(result.note);
      await refresh();
    } catch (cause) {
      setMessage(String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>执行资源</h2>
            <p>
              资源状态（执行端）与值班状态（委托）分开显示。阶段结束不会自动清理资源；
              取消值班不会关闭资源。
            </p>
          </div>
          <div className="resource-actions">
            <button onClick={() => void refresh()} disabled={busy}>
              刷新
            </button>
            <button className="danger" onClick={() => void terminateAll()} disabled={busy}>
              终止任务（关闭全部资源）
            </button>
          </div>
        </div>
        {error && <p className="body-error">{error}</p>}
        {data?.executor_note && <p className="tool-note">{data.executor_note}</p>}
        {message && <p className="tool-note">{message}</p>}

        <h3>待完成工作</h3>
        {pending.length === 0 ? (
          <p className="muted">没有未完成的命令/脚本任务。</p>
        ) : (
          pending.map((item) => (
            <ResourceEntry
              key={item.resource_id}
              sessionId={sessionId}
              resource={item}
              onChanged={() => void refresh(false)}
            />
          ))
        )}

        <h3>保留资源（阶段结束仍存活）</h3>
        {retained.length === 0 ? (
          <p className="muted">没有保留中的执行资源。</p>
        ) : (
          retained.map((item) => (
            <ResourceEntry
              key={item.resource_id}
              sessionId={sessionId}
              resource={item}
              onChanged={() => void refresh(false)}
            />
          ))
        )}

        <h3>本阶段结果（已结束的资源）</h3>
        {finished.length === 0 ? (
          <p className="muted">还没有已结束的资源。</p>
        ) : (
          finished.slice(0, 20).map((item) => (
            <ResourceEntry
              key={item.resource_id}
              sessionId={sessionId}
              resource={item}
              onChanged={() => void refresh(false)}
            />
          ))
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>值班委托</h2>
            <p>
              只有当前任务明确要求持续处理时才应建立委托（需引用你的原话）。
              取消值班只结束处置，不关闭资源。
            </p>
          </div>
        </div>
        {activeDuties.length === 0 ? (
          <p className="muted">当前没有有效值班：事件会被记录，模型不会自动启动。</p>
        ) : (
          data?.duties.map((duty) => (
            <DutyEntry
              key={duty.delegation_id}
              duty={duty}
              busy={busy}
              onCancel={() => void cancelDuty(duty.delegation_id)}
            />
          ))
        )}
        <details>
          <summary>建立值班（需引用用户原话）</summary>
          <div className="duty-form">
            <label>
              覆盖资源 ID（可空；多个用空格或逗号分隔）
              <input value={targetIds} onChange={(event) => setTargetIds(event.target.value)} />
            </label>
            <label>
              处理目标
              <input value={goal} onChange={(event) => setGoal(event.target.value)} />
            </label>
            <label>
              模式
              <select value={mode} onChange={(event) => setMode(event.target.value as "once" | "continuous")}>
                <option value="once">once（处理一次）</option>
                <option value="continuous">continuous（持续）</option>
              </select>
            </label>
            <label>
              引用你的原话（至少 4 个字符，必须能在最近消息里找到）
              <input value={quote} onChange={(event) => setQuote(event.target.value)} />
            </label>
            <button
              className="primary"
              disabled={busy || quote.trim().length < 4}
              onClick={() => void createDuty()}
            >
              建立值班
            </button>
          </div>
        </details>
      </section>
    </div>
  );
}

function DutyEntry({ duty, busy, onCancel }: { duty: DutyCard; busy: boolean; onCancel: () => void }) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div className={`duty-card state-${duty.status}`}>
      <div className="resource-head">
        <span className="resource-kind">{duty.mode === "continuous" ? "持续值班" : "一次性值班"}</span>
        <span className="resource-state">{duty.status === "active" ? "值班中" : duty.status === "cancelled" ? "已取消" : "已完成"}</span>
        <span className="resource-interaction">revision {duty.revision}</span>
      </div>
      <div className="resource-meta">
        {duty.goal || "（未填写目标）"}
        {(duty.resource_ids ?? []).length > 0 ? ` · 覆盖 ${(duty.resource_ids ?? []).length} 个资源` : ""}
        {(duty.event_filters ?? []).length > 0 ? ` · 事件：${(duty.event_filters ?? []).join("、")}` : ""}
        {` · 已触发 ${duty.triggered_count} 次`}
      </div>
      {duty.status === "active" && (
        <div className="resource-actions">
          {confirming ? (
            <>
              <span className="resource-warn">取消值班不会关闭任何资源。</span>
              <button className="danger" disabled={busy} onClick={onCancel}>
                确认取消值班
              </button>
              <button disabled={busy} onClick={() => setConfirming(false)}>
                保留
              </button>
            </>
          ) : (
            <button disabled={busy} onClick={() => setConfirming(true)}>
              取消值班
            </button>
          )}
        </div>
      )}
    </div>
  );
}
