import type { SessionGraphDocument } from "./graphSchema";

export const SMALL_GRAPH: SessionGraphDocument = {
  revision: 17,
  information_graph: {
    nodes: ["A", ...Array.from({ length: 8 }, (_, i) => "neighbor-" + (i + 1)), "hop-two", "other-a", "other-b", "isolated"]
      .map((id, i) => ({ id, label: id === "isolated" ? "独立节点" : "实体 " + id, source_refs: i === 0 ? ["artifact:00000000-0000-0000-0000-000000000001"] : [] })),
    edges: ([
      ...Array.from({ length: 8 }, (_, i): [string, string] => ["A", "neighbor-" + (i + 1)]),
      ["neighbor-1", "hop-two"], ["other-a", "other-b"], ["A", "A"], ["A", "A"],
    ] satisfies [string, string][]).map(([from_id, to_id], i) => ({ id: "r" + i, from_id, to_id, relation: "关系 " + i, status: "observed", source_refs: [] })),
  },
  attack_graph: {
    nodes: [
      { id: "state-1", summary: "状态一", entity_refs: ["A"] },
      { id: "state-2", summary: "状态二", entity_refs: ["hop-two"] },
      { id: "state-3", summary: "独立状态", entity_refs: [] },
    ],
    edges: ([["state-1", "state-2"], ["state-1", "state-1"], ["state-1", "state-1"]] satisfies [string, string][]).map(([from_id, to_id], i) => ({
      id: "t" + i, from_id, to_id, action: "尝试 " + i, outcome: "unknown",
      source_refs: ["operation:00000000-0000-0000-0000-000000000001"],
    })),
  },
};

export function largeGraph(): SessionGraphDocument {
  return {
    revision: 19,
    information_graph: {
      nodes: Array.from({ length: 1000 }, (_, i) => ({ id: "e" + i, label: "实体 " + i, source_refs: [] })),
      edges: Array.from({ length: 999 }, (_, i) => ({
        id: "r" + i, from_id: "e" + i, to_id: "e" + (i + 1), relation: "关联 " + i, status: "observed", source_refs: [],
      })),
    },
    attack_graph: {
      nodes: Array.from({ length: 500 }, (_, i) => ({ id: "s" + i, summary: "状态 " + i, entity_refs: ["e" + i] })),
      edges: Array.from({ length: 499 }, (_, i) => ({
        id: "t" + i, from_id: "s" + i, to_id: "s" + (i + 1), action: "转换 " + i, outcome: "unknown",
        source_refs: ["operation:00000000-0000-0000-0000-000000000001"],
      })),
    },
  };
}
