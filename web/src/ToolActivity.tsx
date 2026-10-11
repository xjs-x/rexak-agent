import { useEffect, useState } from "react";
import { api, type ToolActivityDetail, type ToolActivityItem } from "./api/client";

const titles: Record<string, string> = {
  http_request: "请求网页", browser_navigate: "打开网页", browser_snapshot: "查看页面结构",
  browser_text: "读取页面正文", browser_click: "点击页面", browser_fill: "填写页面",
  browser_screenshot: "保存页面截图", read_artifact: "读取工件", list_files: "查看文件",
  read_file: "读取文件", write_file: "写入文件", edit_file: "修改文件",
  get_job: "检查后台任务", read_job_output: "读取任务输出", search_memory: "检索记忆",
  update_plan: "更新计划", propose_finding: "记录候选发现", propose_completion: "检查阶段结果",
};
const states: Record<string, string> = {
  preparing: "正在处理", accepted: "等待结果", received: "已受理", starting: "正在启动",
  running: "正在执行", succeeded: "已完成", completed: "已返回", failed: "失败",
  cancelled: "已取消", interrupted: "已打断等待", unknown: "状态待核对",
};

function elapsed(item: ToolActivityItem): string {
  if (!item.finished_at && ["failed", "succeeded", "completed", "interrupted", "cancelled"].includes(item.status)) return "耗时未记录";
  const ms = (item.finished_at ? Date.parse(item.finished_at) : Date.now()) - Date.parse(item.started_at);
  if (!Number.isFinite(ms) || ms < 0) return "";
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} 秒`;
}

export function ToolActivityGroup({ sessionId, items }: { sessionId: string; items: ToolActivityItem[] }) {
  const [open, setOpen] = useState(true);
  const active = items.filter((item) => ["preparing", "accepted", "received", "starting", "running"].includes(item.status)).length;
  const failed = items.filter((item) => item.status === "failed").length;
  return <details className="tool-group" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary aria-label="展开或收起工具操作">
      <span>工具操作 · {items.length} 项</span>
      {active > 0 && <span className="tool-group-status">{active} 项进行中</span>}
      {failed > 0 && <span className="tool-group-failed">{failed} 项失败</span>}
      <span className="tool-group-toggle">{open ? "收起" : "展开"}</span>
    </summary>
    <div className="tool-group-items">
      {items.map((item) => <ToolActivity key={`${item.run_id}:${item.tool_call_id}`} sessionId={sessionId} item={item} />)}
    </div>
  </details>;
}

export function ToolActivity({ sessionId, item }: { sessionId: string; item: ToolActivityItem }) {
  const [detail, setDetail] = useState<ToolActivityDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  // 任务返回后，展开的内容须重读；失败可重试，网络故障不能显示成工具失败。
  const [loadedVersion, setLoadedVersion] = useState("");
  const version = `${item.status}:${item.finished_at}:${item.job_state}`;
  const load = async () => {
    if (loading || (detail && loadedVersion === version)) return;
    setLoading(true); setError("");
    try {
      const result = await api.toolActivityDetail(sessionId, item);
      setDetail(result); setLoadedVersion(version);
    } catch (cause) { setError(String(cause)); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (open) void load(); }, [open, version]);
  return <details className={`tool-activity status-${item.status}`} onToggle={(event) => {
    const expanded = event.currentTarget.open;
    setOpen(expanded);
  }}>
    <summary aria-label={`查看工具操作 ${item.tool_name}`}>
      <span className="tool-symbol" aria-hidden="true">⌘</span>
      <span className="tool-title">{titles[item.tool_name] ?? "调用工具"}</span>
      <span className="tool-name">{item.tool_name}</span>
      <span className="tool-state">{states[item.status] ?? item.status}</span>
      <span className="tool-duration">{elapsed(item)}</span>
    </summary>
    {open && <div className="tool-detail">
      <div className="tool-meta">{new Date(item.started_at).toLocaleString("zh-CN", { hour12: false })}
        {item.job_state && ` · 执行端：${states[item.job_state] ?? item.job_state}`}
        {item.sync_status && item.sync_status !== "in_sync" && " · 状态同步待核对"}</div>
      {loading && <p role="status">正在读取操作详情…</p>}
      {error && <p className="body-error">详情读取失败：{error}</p>}
      {detail && <>
        <details className="tool-section"><summary>调用参数</summary><pre>{detail.request_text}</pre></details>
        <details className="tool-section"><summary>返回结果</summary><pre>{detail.result_text}</pre></details>
        <p className="tool-note">{detail.note}</p>
      </>}
      {item.job_id && <div className="tool-reference">任务：{item.job_id}</div>}
      {item.operation_id && <div className="tool-reference">操作：{item.operation_id}</div>}
      {(error || loadedVersion !== version) && <button disabled={loading} onClick={() => void load()}>{loading ? "读取中…" : "刷新详情"}</button>}
    </div>}
  </details>;
}
