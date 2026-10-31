/** Session 图的真实只读 API 数据源；不解析工具回执，不修改图。 */

import { MOCK_SESSION_GRAPH } from "./graphMock";
import type {
  AttemptEdge,
  AttemptOutcome,
  AttackNode,
  InformationNode,
  RelationEdge,
  RelationStatus,
  SessionGraphDocument,
} from "./graphSchema";

/** 当前 Session 的只读图接口。 */
export const GRAPH_ENDPOINT = (sessionId: string) => `/api/sessions/${sessionId}/graph`;

const RELATION_STATUSES: RelationStatus[] = ["observed", "hypothesis", "refuted", "superseded"];
const ATTEMPT_OUTCOMES: AttemptOutcome[] = ["success", "negative", "blocked", "unknown"];

/** 接口返回不符合契约时抛这个，页面会把它当成错误而不是猜字段。 */
export class GraphPayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GraphPayloadError";
  }
}

function asRecord(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new GraphPayloadError(`${path} 应为对象`);
  }
  return value as Record<string, unknown>;
}

function asArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new GraphPayloadError(`${path} 应为数组`);
  return value;
}

function asString(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new GraphPayloadError(`${path} 应为非空字符串`);
  }
  return value;
}

/** 契约里 `source_refs` / `entity_refs` 有默认空数组，缺失时按空处理。 */
function asStringArray(value: unknown, path: string): string[] {
  if (value === undefined || value === null) return [];
  return asArray(value, path).map((item, index) => asString(item, `${path}[${index}]`));
}

function asEnum<T extends string>(value: unknown, allowed: readonly T[], path: string): T {
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    throw new GraphPayloadError(`${path} 只能是 ${allowed.join(" / ")}`);
  }
  return value as T;
}

function asNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new GraphPayloadError(`${path} 应为数字`);
  }
  return value;
}

function parseInformationNode(raw: unknown, path: string): InformationNode {
  const item = asRecord(raw, path);
  return {
    id: asString(item.id, `${path}.id`),
    label: asString(item.label, `${path}.label`),
    source_refs: asStringArray(item.source_refs, `${path}.source_refs`),
  };
}

function parseRelationEdge(raw: unknown, path: string): RelationEdge {
  const item = asRecord(raw, path);
  return {
    id: asString(item.id, `${path}.id`),
    from_id: asString(item.from_id, `${path}.from_id`),
    to_id: asString(item.to_id, `${path}.to_id`),
    relation: asString(item.relation, `${path}.relation`),
    status: asEnum(item.status, RELATION_STATUSES, `${path}.status`),
    source_refs: asStringArray(item.source_refs, `${path}.source_refs`),
  };
}

function parseAttackNode(raw: unknown, path: string): AttackNode {
  const item = asRecord(raw, path);
  return {
    id: asString(item.id, `${path}.id`),
    summary: asString(item.summary, `${path}.summary`),
    entity_refs: asStringArray(item.entity_refs, `${path}.entity_refs`),
  };
}

function parseAttemptEdge(raw: unknown, path: string): AttemptEdge {
  const item = asRecord(raw, path);
  return {
    id: asString(item.id, `${path}.id`),
    from_id: asString(item.from_id, `${path}.from_id`),
    to_id: asString(item.to_id, `${path}.to_id`),
    action: asString(item.action, `${path}.action`),
    outcome: asEnum(item.outcome, ATTEMPT_OUTCOMES, `${path}.outcome`),
    source_refs: asStringArray(item.source_refs, `${path}.source_refs`),
  };
}

/**
 * 把接口返回规整成 `SessionGraphDocument`。
 * 严格校验：字段缺失或枚举取值不认识就抛错，不做默认值兜底——
 * 猜字段会让界面显示一个后端根本没说的结论。
 */
export function normalizeGraphPayload(raw: unknown): SessionGraphDocument {
  const root = asRecord(raw, "$");
  const information = asRecord(root.information_graph, "$.information_graph");
  const attack = root.attack_graph === undefined ? { nodes: [], edges: [] } : asRecord(root.attack_graph, "$.attack_graph");

  return {
    revision: asNumber(root.revision, "$.revision"),
    information_graph: {
      nodes: asArray(information.nodes, "$.information_graph.nodes").map((item, index) =>
        parseInformationNode(item, `$.information_graph.nodes[${index}]`),
      ),
      edges: asArray(information.edges, "$.information_graph.edges").map((item, index) =>
        parseRelationEdge(item, `$.information_graph.edges[${index}]`),
      ),
    },
    attack_graph: {
      nodes: asArray(attack.nodes, "$.attack_graph.nodes").map((item, index) =>
        parseAttackNode(item, `$.attack_graph.nodes[${index}]`),
      ),
      edges: asArray(attack.edges, "$.attack_graph.edges").map((item, index) =>
        parseAttemptEdge(item, `$.attack_graph.edges[${index}]`),
      ),
    },
  };
}

/**
 * 首屏状态。没有会话时直接给演示数据，避免先闪一下「读取中」；
 * 有会话时先 loading，等接口回来再定。
 */
export function initialGraphSource(sessionId: string | null): GraphSource {
  return sessionId
    ? { status: "loading", reason: "正在读取图数据" }
    : { status: "demo", document: MOCK_SESSION_GRAPH, reason: "尚未选择会话" };
}

export type GraphSource =
  | { status: "loading"; reason: string }
  | { status: "remote"; document: SessionGraphDocument; attackDisabled?: boolean }
  | { status: "demo"; document: SessionGraphDocument; reason: string }
  | { status: "empty"; reason: string }
  | { status: "error"; message: string };

/**
 * 读取当前 Session 的双图。
 *
 * - 没有选中会话：用演示数据，并说明原因；
 * - 真实会话接口失败：显示错误，不展示样例；
 * - 200 但结构不符：报错，不猜；
 * - 200 且两张图都为空：空态；
 * - 网络失败：报错，给出可重试提示。
 */
export async function loadSessionGraph(
  sessionId: string | null,
  signal?: AbortSignal,
): Promise<GraphSource> {
  if (!sessionId) {
    return { status: "demo", document: MOCK_SESSION_GRAPH, reason: "尚未选择会话" };
  }

  let response: Response;
  try {
    // GET /api/sessions/{session_id}/graph，使用同源会话凭据，只读且不缓存。
    response = await fetch(GRAPH_ENDPOINT(sessionId), {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    return { status: "error", message: "无法连接本机服务，图数据未读取。" };
  }

  if (!response.ok) {
    return { status: "error", message: `读取图数据失败（HTTP ${response.status}）。` };
  }

  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    return { status: "error", message: "接口返回的不是合法 JSON。" };
  }

  try {
    const document = normalizeGraphPayload(raw);
    if (!document.information_graph.nodes.length && !document.attack_graph.nodes.length) {
      return { status: "empty", reason: "这个会话还没有登记任何图对象。" };
    }
    return { status: "remote", document, attackDisabled: !(raw as Record<string, unknown>).attack_graph };
  } catch (cause) {
    return {
      status: "error",
      message: cause instanceof Error ? cause.message : String(cause),
    };
  }
}
