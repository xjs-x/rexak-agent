import { expect, test } from "vitest";
import { buildGraphFlow, NODE_HEIGHT, NODE_WIDTH, type FlowNode } from "./graphLayout";
import { largeGraph, SMALL_GRAPH } from "./graphTestFixtures";

function expectNonOverlapping(nodes: FlowNode[]) {
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i]!.position;
      const b = nodes[j]!.position;
      if (Math.abs(a.x - b.x) < NODE_WIDTH && Math.abs(a.y - b.y) < NODE_HEIGHT) {
        throw new Error('Overlapping nodes: ' + nodes[i]!.id + ', ' + nodes[j]!.id);
      }
    }
  }
}

test("信息图完整纳入 13 个节点和 12 条边，包括独立分量和二跳节点", () => {
  const flow = buildGraphFlow(SMALL_GRAPH, "information");
  expect(flow.nodes.map(node => node.id).sort()).toEqual(SMALL_GRAPH.information_graph.nodes.map(node => node.id).sort());
  expect(flow.edges.map(edge => edge.id).sort()).toEqual(SMALL_GRAPH.information_graph.edges.map(edge => edge.id).sort());
  expect(flow.components).toBe(3);
  expect(flow.incident.get("isolated")).toEqual([]);
  expect(flow.edgeById.get("r10")?.data?.path).not.toBe(flow.edgeById.get("r11")?.data?.path);
  expectNonOverlapping(flow.nodes);
});

test("攻击图完整纳入 3 个状态和 3 条边，状态引用保持原值", () => {
  const flow = buildGraphFlow(SMALL_GRAPH, "attack");
  expect(flow.nodes).toHaveLength(3);
  expect(flow.edges).toHaveLength(3);
  expect(flow.components).toBe(2);
  expect(flow.nodeById.get("state-1")?.data.object).toBe(SMALL_GRAPH.attack_graph.nodes[0]);
  expect(flow.edgeById.get("t1")?.data?.path).not.toBe(flow.edgeById.get("t2")?.data?.path);
  expectNonOverlapping(flow.nodes);
});

test("双向和平行边共享唯一节点，各自拥有可区分的路径", () => {
  const document = structuredClone(SMALL_GRAPH);
  const base = document.information_graph.edges[0]!;
  document.information_graph.edges.push({ ...base, id: "parallel" }, {
    ...base, id: "reverse", from_id: base.to_id, to_id: base.from_id,
  });
  const flow = buildGraphFlow(document, "information");
  expect(flow.nodes).toHaveLength(13);
  expect(new Set(flow.nodes.map(node => node.id)).size).toBe(13);
  const paths = [base.id, "parallel", "reverse"].map(id => flow.edgeById.get(id)?.data?.path);
  expect(paths.every(Boolean)).toBe(true);
  expect(new Set(paths).size).toBe(3);
  expect(flow.edgeById.get("reverse")?.target).toBe("A");
});

test("重复节点、重复边及悬空边可诊断，现有节点位置不会被覆盖", () => {
  const document = structuredClone(SMALL_GRAPH);
  const base = document.information_graph.edges[0]!;
  document.information_graph.nodes.push({ id: "A", label: "重复记录", source_refs: [] });
  document.information_graph.edges.push(base, { ...base, id: "dangling", to_id: "absent" });
  const flow = buildGraphFlow(document, "information");
  const original = buildGraphFlow(SMALL_GRAPH, "information");
  expect(flow.invalidEdges).toBe(1);
  expect(flow.duplicateNodes).toBe(1);
  expect(flow.duplicateEdges).toBe(1);
  expect(flow.nodes).toEqual(original.nodes);
  expect(flow.edges).toEqual(original.edges);
});

test("重新读取及调整文档顺序都不改变同一组对象的坐标", () => {
  const document = structuredClone(SMALL_GRAPH);
  document.revision += 1;
  document.information_graph.nodes.reverse();
  document.information_graph.edges.reverse();
  expect(buildGraphFlow(document, "information")).toEqual(buildGraphFlow(SMALL_GRAPH, "information"));
});

test("大图布局纳入所有节点和边，节点矩形无重叠", () => {
  const document = largeGraph();
  const information = buildGraphFlow(document, "information");
  const attack = buildGraphFlow(document, "attack");
  expect([information.nodes.length, information.edges.length]).toEqual([1000, 999]);
  expect([attack.nodes.length, attack.edges.length]).toEqual([500, 499]);
  expectNonOverlapping(information.nodes);
  expectNonOverlapping(attack.nodes);
});
