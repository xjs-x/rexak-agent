/**
 * Session 双图数据形状的 TypeScript 镜像。
 *
 * 权威定义在 Python 侧，本文件只做镜像，不新增任何业务字段：
 * - 节点/边契约：`src/agent_workbench/contracts/session_graph.py`
 * - 文档存储形状：`src/agent_workbench/domain/session_graph.py`
 *   （`empty_graph` / `apply_operations` 决定 `{id, ...data}` 的落库形状）
 *
 * 落库后的四个对象（字段名与后端完全一致）：
 * - information_graph.nodes → `{id, label, source_refs}`
 * - information_graph.edges → `{id, from_id, to_id, relation, status, source_refs}`
 * - attack_graph.nodes      → `{id, summary, entity_refs}`
 * - attack_graph.edges      → `{id, from_id, to_id, action, outcome, source_refs}`
 *
 * 刻意没有的东西（后端没有定义，前端不得虚构）：
 * - Entity 没有类别 / 严重等级 / 置信度字段，只有 `label` 与 `source_refs`。
 * - AttackState 没有 `label`、没有 `source_refs`，证据只能挂在 Attempt 上。
 * - 两张图之间没有图级边；Entity 只能通过 `AttackState.entity_refs` 与攻击状态关联，
 *   且 Entity 不能作为 Attempt 的端点。
 *
 * 当前只读查询为 GET /api/sessions/{session_id}/graph，与 Agent 的 read_graph 同源。
 */

/** `Relation.status`，对应 Python `Literal["observed", "hypothesis", "refuted", "superseded"]`。 */
export type RelationStatus = "observed" | "hypothesis" | "refuted" | "superseded";

/** `Attempt.outcome`，对应 Python `Literal["success", "negative", "blocked", "unknown"]`。 */
export type AttemptOutcome = "success" | "negative" | "blocked" | "unknown";

/**
 * 来源引用：`operation:<UUID>` 或 `artifact:<UUID>`。
 * 后端 `validate_sources` 只接受这两种前缀，且必须属于当前 Session。
 */
export type SourceRef = string;

/** 信息图节点：`Entity`。只有短标签与证据来源，没有类别字段。 */
export type InformationNode = {
  id: string;
  label: string;
  source_refs: SourceRef[];
};

/** 信息图边：`Relation`。端点只能是 Entity。 */
export type RelationEdge = {
  id: string;
  from_id: string;
  to_id: string;
  relation: string;
  status: RelationStatus;
  source_refs: SourceRef[];
};

/** 攻击图节点：`AttackState`。只有摘要与关联实体，自身不携带证据来源。 */
export type AttackNode = {
  id: string;
  summary: string;
  entity_refs: string[];
};

/** 攻击图边：`Attempt`。端点只能是 AttackState，允许自环；`source_refs` 至少 1 项。 */
export type AttemptEdge = {
  id: string;
  from_id: string;
  to_id: string;
  action: string;
  outcome: AttemptOutcome;
  source_refs: SourceRef[];
};

export type GraphSlice<N, E> = { nodes: N[]; edges: E[] };

/**
 * `read_graph` 的返回体（未含 `get_graph` 额外附加的 `pending_page`）。
 * `revision` 是图版本，由程序维护，模型写入不回传该值。
 */
export type SessionGraphDocument = {
  revision: number;
  information_graph: GraphSlice<InformationNode, RelationEdge>;
  attack_graph: GraphSlice<AttackNode, AttemptEdge>;
};

export type GraphKey = "information" | "attack";
