/**
 * 双图文档的汇总统计。
 *
 * 全部是**从文档本身算出来的**，不引入任何新的业务判断：
 * - 关系状态、尝试结果的计数直接来自枚举；
 * - 「需要注意」的每一条都对应一个可核对的事实（缺字段、度数为零），
 *   不是模型给出的风险评级——后端也没有这种字段。
 */

import type {
  AttemptEdge,
  AttemptOutcome,
  AttackNode,
  InformationNode,
  RelationEdge,
  RelationStatus,
  SessionGraphDocument,
} from "./graphSchema";

export const RELATION_STATUS_VALUES: RelationStatus[] = [
  "observed",
  "hypothesis",
  "refuted",
  "superseded",
];

export const ATTEMPT_OUTCOME_VALUES: AttemptOutcome[] = [
  "success",
  "negative",
  "blocked",
  "unknown",
];

export type DocumentStats = {
  entities: number;
  relations: number;
  states: number;
  attempts: number;
  /** 自环尝试数：`from_id === to_id`。 */
  loops: number;
  relationStatus: Record<RelationStatus, number>;
  outcome: Record<AttemptOutcome, number>;

  /** 没有任何 `source_refs` 的实体。 */
  entitiesWithoutEvidence: InformationNode[];
  /** 度数为零的实体（既没有入边也没有出边）。 */
  relationlessEntities: InformationNode[];
  /** `entity_refs` 为空的攻击状态。 */
  statesWithoutEntities: AttackNode[];
  /** 状态是假设、又没有挂任何来源的关系。 */
  unsourcedHypotheses: RelationEdge[];
  /** 结果为 unknown 的尝试（既不能证明成功，也不能证明没发生）。 */
  undecidedAttempts: AttemptEdge[];
};

function emptyTally<T extends string>(values: T[]): Record<T, number> {
  return values.reduce((acc, key) => ({ ...acc, [key]: 0 }), {} as Record<T, number>);
}

export function summarize(document: SessionGraphDocument): DocumentStats {
  const nodes = document.information_graph.nodes;
  const relations = document.information_graph.edges;
  const states = document.attack_graph.nodes;
  const attempts = document.attack_graph.edges;

  const relationStatus = emptyTally(RELATION_STATUS_VALUES);
  for (const edge of relations) {
    if (edge.status in relationStatus) relationStatus[edge.status] += 1;
  }

  const outcome = emptyTally(ATTEMPT_OUTCOME_VALUES);
  for (const edge of attempts) {
    if (edge.outcome in outcome) outcome[edge.outcome] += 1;
  }

  const degree = new Map<string, number>();
  for (const node of nodes) degree.set(node.id, 0);
  for (const edge of relations) {
    if (degree.has(edge.from_id)) degree.set(edge.from_id, (degree.get(edge.from_id) ?? 0) + 1);
    if (degree.has(edge.to_id)) degree.set(edge.to_id, (degree.get(edge.to_id) ?? 0) + 1);
  }

  return {
    entities: nodes.length,
    relations: relations.length,
    states: states.length,
    attempts: attempts.length,
    loops: attempts.filter((edge) => edge.from_id === edge.to_id).length,
    relationStatus,
    outcome,
    entitiesWithoutEvidence: nodes.filter((node) => node.source_refs.length === 0),
    relationlessEntities: nodes.filter((node) => (degree.get(node.id) ?? 0) === 0),
    statesWithoutEntities: states.filter((state) => state.entity_refs.length === 0),
    unsourcedHypotheses: relations.filter(
      (edge) => edge.status === "hypothesis" && edge.source_refs.length === 0,
    ),
    undecidedAttempts: attempts.filter((edge) => edge.outcome === "unknown"),
  };
}

/** 取最大值用于画条形，至少为 1，避免除零。 */
export function maxOf(values: number[]): number {
  return Math.max(1, ...values);
}
