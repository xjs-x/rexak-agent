/**
 * 双图的统一中文显示映射。
 *
 * 规则：底层字段名、ID、类型、引用关系一律保持后端原样；
 * 这里只提供「显示名 / 说明 / 视觉档位」，组件不得再各自硬编码中文。
 *
 * 措辞底线（来自 contracts/session_graph.py 与 tools/session_graph.py）：
 * - 整张图是模型维护的工作资料，`update_graph` 回执里写的是
 *   `semantic_verification: "model_judgment"`，所以任何一处都不能显示成「已验证」。
 * - `Relation.status = observed` 只表示来源可追溯，不等于关系语义被独立验证。
 * - `Attempt.outcome = success` 只表示「这一次尝试达成了它自己的预期」，
 *   不等于漏洞成立，更不等于攻击成功。
 */

import type { AttemptOutcome, GraphKey, RelationStatus } from "./graphSchema";

export const GRAPH_TITLES: Record<GraphKey, string> = {
  information: "信息关系图",
  attack: "攻击状态图",
};

/** 概览页的标题；它不是一张图，所以单独放在这里，不和 GraphKey 混。 */
export const OVERVIEW_TITLE = "概览";
export const OVERVIEW_CAPTION = "整张图的数量、状态分布与字段缺口";

export const GRAPH_CAPTIONS: Record<GraphKey, string> = {
  information: "实体，以及实体之间的关联关系",
  attack: "已观察到的攻击状态，以及在状态之间发生过的测试尝试",
};

/** 对象种类名：同一张图里「点」和「线」分别叫什么。 */
export const OBJECT_KIND_LABELS: Record<GraphKey, { node: string; edge: string }> = {
  information: { node: "实体", edge: "关联关系" },
  attack: { node: "攻击状态", edge: "测试尝试" },
};

/**
 * 字段级显示名。键就是后端字段名，值只是展示文案。
 * 用字面量类型而不是 `Record<string, string>`，
 * 这样调用方拿到的是确定的字符串，不必再处理 undefined。
 */
export const FIELD_LABELS = {
  id: "对象 ID",
  label: "实体名称",
  summary: "状态描述",
  relation: "关联关系",
  status: "关系状态",
  action: "尝试动作",
  outcome: "尝试结果",
  source_refs: "证据来源",
  entity_refs: "关联实体",
  from_id: "起点",
  to_id: "终点",
  revision: "图版本",
} as const;

/** 动态字段名（例如遍历未知键）时的安全取值。 */
export function fieldLabel(field: string): string {
  const table: Record<string, string> = FIELD_LABELS;
  return table[field] ?? field;
}

/** 关系状态：四个值都是后端 `Relation.status` 的真实枚举。 */
export const RELATION_STATUS_LABELS: Record<RelationStatus, string> = {
  observed: "已观察",
  hypothesis: "假设",
  refuted: "已否定",
  superseded: "已被取代",
};

export const RELATION_STATUS_HINTS: Record<RelationStatus, string> = {
  observed: "关系在真实回执中有来源可追溯；关系语义仍是模型判断，不是独立验证。",
  hypothesis: "模型提出、尚待验证的关联，不能当作已知事实使用。",
  refuted: "已被后续证据否定，保留在这里用于说明排除过程。",
  superseded: "已被更新的关系判断取代，保留历史版本。",
};

/** 尝试结果：四个值都是后端 `Attempt.outcome` 的真实枚举。 */
export const ATTEMPT_OUTCOME_LABELS: Record<AttemptOutcome, string> = {
  success: "达到该次预期",
  negative: "未达到该次预期",
  blocked: "被阻断",
  unknown: "结果无法确认",
};

export const ATTEMPT_OUTCOME_HINTS: Record<AttemptOutcome, string> = {
  success: "只表示这一次尝试达成了它自己的预期结果，不代表漏洞已成立或已被独立验证。",
  negative: "这一次尝试没有达到预期；不能据此推断目标不存在问题。",
  blocked: "执行被拒绝或中断，没有取得可判定结果。",
  unknown: "现有回执无法确认结果；既不能证明发生成功，也不能证明没有发生。",
};

/** 视觉档位：把枚举映射到 CSS 类后缀，避免组件里写死颜色判断。 */
export const RELATION_STATUS_TONES: Record<RelationStatus, string> = {
  observed: "observed",
  hypothesis: "hypothesis",
  refuted: "refuted",
  superseded: "superseded",
};

export const ATTEMPT_OUTCOME_TONES: Record<AttemptOutcome, string> = {
  success: "success",
  negative: "negative",
  blocked: "blocked",
  unknown: "unknown",
};

/**
 * 描边色。CSS（graph.css 的 `.edge-*`）与连线箭头共用这一份值，
 * 因为 React Flow 的 marker 只能用具体颜色，不能读 CSS 变量。
 */
export const TONE_COLORS: Record<string, string> = {
  observed: "#4c6f93",
  hypothesis: "#a8761c",
  refuted: "#b42318",
  superseded: "#9a9aa2",
  success: "#17795a",
  negative: "#5b6b7c",
  blocked: "#a8761c",
  unknown: "#8a8a92",
};

/** 来源引用的两类前缀，对应后端 `validate_sources` 只接受的两种 ref。 */
export type SourceKind = "operation" | "artifact" | "unknown";

export const SOURCE_KIND_LABELS: Record<SourceKind, string> = {
  operation: "工具操作",
  artifact: "证据工件",
  unknown: "无法识别的来源",
};

export const SOURCE_KIND_HINTS: Record<SourceKind, string> = {
  operation: "指向当前 Session 真实执行回执中的 Operation。",
  artifact: "指向当前 Session 中可读取的工件。",
  unknown: "格式不符合 operation:<UUID> 或 artifact:<UUID>。",
};

export type ParsedSourceRef = {
  raw: string;
  kind: SourceKind;
  label: string;
  uuid: string;
};

/**
 * 解析 `operation:<UUID>` / `artifact:<UUID>`。
 * 不认识的格式原样保留，不猜测其含义。
 */
export function parseSourceRef(raw: string): ParsedSourceRef {
  const separator = raw.indexOf(":");
  if (separator <= 0) {
    return { raw, kind: "unknown", label: SOURCE_KIND_LABELS.unknown, uuid: raw };
  }
  const prefix = raw.slice(0, separator);
  const uuid = raw.slice(separator + 1);
  if (prefix === "operation" || prefix === "artifact") {
    return { raw, kind: prefix, label: SOURCE_KIND_LABELS[prefix], uuid };
  }
  return { raw, kind: "unknown", label: SOURCE_KIND_LABELS.unknown, uuid };
}

/** UUID 太长，列表里只显示首尾；完整值放在 title 与详情面板里。 */
export function shortId(value: string, keep = 8): string {
  if (value.length <= keep * 2 + 1) return value;
  return `${value.slice(0, keep)}…${value.slice(-4)}`;
}

export function statusLabel(status: string): string {
  return RELATION_STATUS_LABELS[status as RelationStatus] ?? status;
}

export function outcomeLabel(outcome: string): string {
  return ATTEMPT_OUTCOME_LABELS[outcome as AttemptOutcome] ?? outcome;
}

/** `update_graph` 回执里的固定值，界面据此提示「图内容是模型判断」。 */
export const SEMANTIC_VERIFICATION_LABELS: Record<string, string> = {
  model_judgment: "模型判断",
};
