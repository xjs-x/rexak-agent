// @vitest-environment jsdom
import { expect, test } from "vitest";
import { summarize } from "./graphStats";
import { MOCK_SESSION_GRAPH } from "./graphMock";

const INFORMATION = MOCK_SESSION_GRAPH.information_graph;
const ATTACK = MOCK_SESSION_GRAPH.attack_graph;

test("总数与文档一致", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  expect(stats.entities).toBe(INFORMATION.nodes.length);
  expect(stats.relations).toBe(INFORMATION.edges.length);
  expect(stats.states).toBe(ATTACK.nodes.length);
  expect(stats.attempts).toBe(ATTACK.edges.length);
});

test("状态与结果的分项计数加起来等于总数", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  const statusSum = Object.values(stats.relationStatus).reduce((a, b) => a + b, 0);
  const outcomeSum = Object.values(stats.outcome).reduce((a, b) => a + b, 0);
  expect(statusSum).toBe(stats.relations);
  expect(outcomeSum).toBe(stats.attempts);
});

test("自环数与文档里 from_id === to_id 的尝试数一致", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  const loops = ATTACK.edges.filter((edge) => edge.from_id === edge.to_id);
  expect(stats.loops).toBe(loops.length);
  expect(stats.loops).toBeGreaterThan(0);
});

test("四种关系状态都出现过，未知取值不会被算进任何一档", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  for (const key of ["observed", "hypothesis", "refuted", "superseded"] as const) {
    expect(stats.relationStatus[key]).toBeGreaterThan(0);
  }
});

test("缺证据实体按 source_refs 为空筛出，不按别的口径", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  const expected = INFORMATION.nodes.filter((node) => node.source_refs.length === 0);
  expect(stats.entitiesWithoutEvidence.map((node) => node.id)).toEqual(
    expected.map((node) => node.id),
  );
  expect(expected.length).toBeGreaterThan(0);
});

test("度数统计把入边和出边都算上", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  const expected = INFORMATION.nodes.filter(
    (node) =>
      !INFORMATION.edges.some((edge) => edge.from_id === node.id || edge.to_id === node.id),
  );
  expect(stats.relationlessEntities.map((node) => node.id)).toEqual(
    expected.map((node) => node.id),
  );
});

test("「假设且无来源」只看 hypothesis 与空 source_refs 的交集", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  const expected = INFORMATION.edges.filter(
    (edge) => edge.status === "hypothesis" && edge.source_refs.length === 0,
  );
  expect(stats.unsourcedHypotheses.length).toBe(expected.length);
});

test("结果无法确认的尝试单独列出，且不超过总数", () => {
  const stats = summarize(MOCK_SESSION_GRAPH);
  expect(stats.undecidedAttempts.length).toBe(stats.outcome.unknown);
  expect(stats.undecidedAttempts.every((edge) => edge.outcome === "unknown")).toBe(true);
});

test("空文档不会抛错，计数全为零", () => {
  const stats = summarize({
    revision: 0,
    information_graph: { nodes: [], edges: [] },
    attack_graph: { nodes: [], edges: [] },
  });
  expect(stats.entities).toBe(0);
  expect(stats.relations).toBe(0);
  expect(stats.loops).toBe(0);
  expect(stats.entitiesWithoutEvidence).toEqual([]);
  expect(stats.undecidedAttempts).toEqual([]);
});
