import type { RunItem, Snapshot } from "../api/client";

/** 真实 Run 状态（contracts/enums.py）。不包含静态稿里的 `unknown`。 */
export const RUN_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_tool: "等待工具结果",
  waiting_user: "等待你的输入",
  pausing: "正在暂停",
  paused: "已暂停",
  cancelling: "正在取消",
  cancelled: "已取消",
  recovering: "等待恢复（需核对）",
  completed: "本阶段已结束",
  failed: "运行失败",
};

export const RUN_TONES: Record<string, string> = {
  queued: "st-idle",
  running: "st-running",
  waiting_tool: "st-running",
  waiting_user: "st-ask",
  pausing: "st-idle",
  paused: "st-idle",
  cancelling: "st-idle",
  cancelled: "st-idle",
  recovering: "st-warn",
  completed: "st-done",
  failed: "st-fail",
};

export const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function runLabel(status: string | null | undefined): string {
  return status ? RUN_LABELS[status] ?? status : "未开始";
}

export function runTone(status: string | null | undefined): string {
  return status ? RUN_TONES[status] ?? "st-idle" : "st-idle";
}

export function canPause(status: string | null | undefined): boolean {
  return ["queued", "running", "waiting_tool", "waiting_user"].includes(status ?? "");
}

export function canResume(status: string | null | undefined): boolean {
  return ["paused", "waiting_user", "waiting_tool", "failed", "recovering"].includes(status ?? "");
}

/** 对话输入框里的蓝色方块：只在实际执行/排队/等工具时出现，点击会暂停本轮。 */
export function canStop(status: string | null | undefined): boolean {
  return ["queued", "running", "waiting_tool"].includes(status ?? "");
}

export function canCancel(status: string | null | undefined): boolean {
  // 已经在取消中就不再重复发取消指令；其余非终态仍可取消。
  return !!status && !TERMINAL.has(status) && status !== "cancelling";
}

export function isActive(status: string | null | undefined): boolean {
  return !!status && !TERMINAL.has(status);
}

/** 测试/评测夹具项目：仅用于默认收起，用户仍可显式显示。 */
export function isFixtureProject(name: string): boolean {
  return /^(test-|memory-distill-|eval-|fixture-|drill-|api-smoke)/i.test(name);
}

export function latestRun(snapshot: Snapshot | null): RunItem | null {
  return snapshot?.runs.at(-1) ?? null;
}

/** 工具操作的中文动作名。与旧控制台保持一致，避免同一工具在两套界面里叫不同名字。 */
export const TOOL_TITLES: Record<string, string> = {
  http_request: "请求网页",
  browser_navigate: "打开网页",
  browser_snapshot: "查看页面结构",
  browser_text: "读取页面正文",
  browser_click: "点击页面",
  browser_fill: "填写页面",
  browser_screenshot: "保存页面截图",
  read_artifact: "读取工件",
  list_attachments: "查看用户附件",
  read_attachment: "读取用户附件",
  search_attachment: "搜索用户附件",
  list_files: "查看文件",
  read_file: "读取文件",
  write_file: "写入文件",
  edit_file: "修改文件",
  edit_file_batch: "批量修改文件",
  glob_files: "查找文件",
  search_files: "检索文件内容",
  get_job: "检查后台任务",
  read_job_output: "读取任务输出",
  search_memory: "检索记忆",
  update_plan: "更新计划",
  propose_finding: "记录候选发现",
  propose_completion: "检查阶段结果",
  search_history: "检索历史",
  record_lead: "更新线索",
  record_observation: "记录观察",
  propose_memory: "记录经验候选",
  record_memory_usage: "记录经验使用反馈",
  run_command: "运行命令",
  run_script: "运行脚本",
  start_monitor: "启动监视",
  shell_open: "打开终端",
  shell_send: "终端输入",
  shell_read: "读取终端",
  shell_resize: "调整终端尺寸",
  shell_list: "列出终端",
  shell_close: "关闭终端",
  list_jobs: "查看执行资源",
  wait_for_resources: "等待资源条件",
  get_execution_environment: "查看执行环境",
  set_execution_duty: "建立值班",
  list_execution_duties: "查看值班",
  cancel_execution_duty: "取消值班",
  list_execution_templates: "查看执行模板",
  get_execution_template: "读取执行模板",
  register_execution_template: "登记执行模板",
  run_execution_template: "运行执行模板",
};

export function toolTitle(name: string): string {
  return TOOL_TITLES[name] ?? "调用工具";
}

const EDIT_TOOLS = new Set(["write_file", "edit_file", "edit_file_batch"]);
const COMMAND_TOOLS = new Set(["run_command", "run_script"]);

export type ToolKind = "edit" | "command" | "other";

export function classifyTool(name: string): ToolKind {
  if (EDIT_TOOLS.has(name)) return "edit";
  if (COMMAND_TOOLS.has(name)) return "command";
  return "other";
}

export const TOOL_STATE_LABELS: Record<string, string> = {
  preparing: "正在处理",
  accepted: "等待结果",
  received: "已受理",
  starting: "正在启动",
  running: "正在执行",
  succeeded: "已完成",
  completed: "已返回",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已打断等待",
  unknown: "状态待核对",
  exited: "已退出",
  closed: "已关闭",
};

export function toolStateLabel(status: string): string {
  return TOOL_STATE_LABELS[status] ?? status;
}

export function toolStateTone(status: string): "done" | "run" | "unknown" | "fail" {
  if (status === "failed") return "fail";
  if (["unknown", "interrupted"].includes(status)) return "unknown";
  if (["running", "starting", "accepted", "received", "preparing"].includes(status)) return "run";
  return "done";
}

export const WEAKNESS_STATUS_LABELS: Record<string, string> = {
  candidate: "待验证",
  confirmed: "已确认",
  disproved: "复测未支持",
  inconclusive: "证据不足",
};

export function weaknessStatusLabel(status: string): string {
  return WEAKNESS_STATUS_LABELS[status] ?? status.replaceAll("_", " ");
}

export function weaknessKindLabel(kind: string | null | undefined): string {
  if (kind === "vulnerability") return "独立缺陷";
  if (kind === "enabling_condition") return "使能条件";
  return "未判定类型";
}

export const LEAD_STATUS_LABELS: Record<string, string> = {
  open: "待跟进",
  active: "推进中",
  blocked: "受阻",
  deferred: "暂缓",
  resolved: "已解决",
  closed: "已关闭",
};

export const ACTIONABILITY_LABELS: Record<string, string> = {
  ready: "可推进",
  needs_condition: "缺条件",
  no_new_action: "无新动作",
  needs_review: "待复查",
};

export const PLAN_STATUS_LABELS: Record<string, string> = {
  pending: "待开始",
  in_progress: "进行中",
  done: "已完成",
  dropped: "已放弃",
  blocked: "受阻",
};

export function modeLabel(mode: string | null | undefined): string {
  return mode === "scoped" ? "严格范围" : "按指令运行";
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

export function formatMessageTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatShortTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function timeAgo(value: string | null | undefined): string {
  if (!value) return "";
  const then = Date.parse(value);
  if (!Number.isFinite(then)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 7) return `${Math.floor(seconds / 86400)} 天前`;
  return formatShortTime(value);
}

export function durationLabel(start: string | null | undefined, end: string | null | undefined): string {
  if (!start || !end) return "";
  const ms = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${ms} 毫秒`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} 秒`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} 分 ${seconds} 秒`;
}

export function shortId(id: string | null | undefined, length = 8): string {
  return id ? id.slice(0, length) : "—";
}

export function responseOutcomeLabel(outcome: string | null | undefined): string | null {
  if (!outcome || outcome === "usable") return null;
  return {
    truncated: "响应不完整（达到输出限制）",
    empty: "响应为空",
    invalid_tool_call: "工具调用不完整",
    refused: "被供应商拒绝",
  }[outcome] ?? "响应不可用";
}

/** 从工具请求里提取可读的命令或路径；不假设统一结构。 */
export function requestSummary(text: string, max = 240): string {
  const raw = text?.trim() ?? "";
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const command = parsed.command ?? parsed.cmd ?? parsed.argv ?? parsed.commands;
    if (typeof command === "string") return truncate(command, max);
    if (Array.isArray(command)) return truncate(command.map(String).join(" "), max);
    const path = parsed.path ?? parsed.file_path ?? parsed.filename ?? parsed.query;
    if (typeof path === "string") return truncate(path, max);
    const url = parsed.url ?? parsed.target ?? parsed.endpoint ?? parsed.href;
    if (typeof url === "string") {
      const method = typeof parsed.method === "string" ? `${parsed.method} ` : "";
      return truncate(`${method}${url}`, max);
    }
  } catch {
    // 非 JSON：保留第一行，避免把大段输出塞进列表。
  }
  return truncate(raw.split("\n").find((line) => line.trim()) ?? raw, max);
}

export function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

export function splitLines(text: string | null | undefined, limit = 500): string[] {
  if (!text) return [];
  return text.replace(/\r\n/g, "\n").split("\n").slice(0, limit);
}

export function textOf(value: unknown, max = 400): string {
  const raw = typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
  return truncate(raw, max);
}
