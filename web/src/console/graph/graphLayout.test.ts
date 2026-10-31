// @vitest-environment jsdom
import { expect, test } from "vitest";
import { buildAttackFlow, buildInformationFlow, FOLD_ID, WING_LIMIT } from "./graphLayout";
import { MOCK_SESSION_GRAPH } from "./graphMock";
import { parseSourceRef } from "./graphLabels";

const INFORMATION = MOCK_SESSION_GRAPH.information_graph;
const ATTACK = MOCK_SESSION_GRAPH.attack_graph;

/** 默认焦点是文档里的第一个对象。 */
const WEB = INFORMATION.nodes[0]!;
const STATE = ATTACK.nodes[0]!;

const idOf = (label: string) => {
  const found = INFORMATION.nodes.find((node) => node.label.includes(label));
  expect(found, `未找到实体：${label}`).toBeTruthy();
  return found!.id;
};

const outOf = (id: string) =>
  INFORMATION.edges.filter((edge) => edge.from_id === id && edge.to_id !== id);
const into = (id: string) =>
  INFORMATION.edges.filter((edge) => edge.to_id === id && edge.from_id !== id);

test("信息图：默认焦点是第一个实体，画布只放焦点与它的一跳邻居", () => {
  const built = buildInformationFlow(MOCK_SESSION_GRAPH, null, null);
  expect(built.focusId).toBe(WEB.id);

  for (const node of built.nodes) {
    if (node.id === WEB.id || node.id === FOLD_ID.inbound || node.id === FOLD_ID.outbound) continue;
    const connected = INFORMATION.edges.some(
      (edge) =>
        (edge.from_id === WEB.id && edge.to_id === node.id) ||
        (edge.to_id === WEB.id && edge.from_id === node.id),
    );
    expect(connected, `${node.id} 与焦点不直接相连`).toBe(true);
  }
});

test("信息图：单侧超过上限时折成「还有 N 个」，不会把一列撑爆", () => {
  const hub = idOf("用户管理接口");
  const built = buildInformationFlow(MOCK_SESSION_GRAPH, hub, null);

  const inbound = into(hub);
  expect(inbound.length).toBeGreaterThan(WING_LIMIT);

  const fold = built.nodes.find((node) => node.id === FOLD_ID.inbound);
  expect(fold).toBeTruthy();
  expect((fold?.data as { hidden: number }).hidden).toBe(inbound.length - WING_LIMIT);

  const drawnInbound = built.nodes.filter(
    (node) => (node.data as { role?: string }).role === "inbound",
  );
  expect(drawnInbound.length).toBe(WING_LIMIT);
  expect(built.hidden.inbound.length).toBe(inbound.length - WING_LIMIT);
  expect(built.inbound).toBe(inbound.length);
});

test("信息图：默认焦点的出边也会触发折叠，且折叠清单里的对象真实存在", () => {
  const built = buildInformationFlow(MOCK_SESSION_GRAPH, null, null);
  const outbound = outOf(WEB.id);
  expect(outbound.length).toBeGreaterThan(WING_LIMIT);

  expect(built.nodes.some((node) => node.id === FOLD_ID.outbound)).toBe(true);
  expect(built.hidden.outbound.length).toBe(outbound.length - WING_LIMIT);
  const ids = new Set(INFORMATION.nodes.map((node) => node.id));
  for (const item of built.hidden.outbound) expect(ids.has(item.id)).toBe(true);
});

test("信息图：左列全是入边来源，右列全是出边目标，不存在跨列连线", () => {
  const focusId = idOf("用户管理接口");
  const built = buildInformationFlow(MOCK_SESSION_GRAPH, focusId, null);

  const roles = new Map(
    built.nodes.map((node) => [node.id, (node.data as { role?: string }).role]),
  );
  expect(roles.get(focusId)).toBe("focus");
  for (const edge of into(focusId).slice(0, WING_LIMIT)) {
    expect(roles.get(edge.from_id)).toBe("inbound");
  }
  for (const edge of outOf(focusId).slice(0, WING_LIMIT)) {
    expect(roles.get(edge.to_id)).toBe("outbound");
  }

  const xOf = (id: string) => built.nodes.find((node) => node.id === id)!.position.x;
  for (const edge of built.edges) {
    const direction = (edge.data as { direction?: string } | undefined)?.direction;
    if (direction === "inbound") {
      expect(edge.target).toBe(focusId);
      expect(xOf(edge.source)).toBeLessThan(xOf(focusId));
    } else if (direction === "outbound") {
      expect(edge.source).toBe(focusId);
      expect(xOf(edge.target)).toBeGreaterThan(xOf(focusId));
    } else {
      expect(edge.source).toBe(edge.target);
    }
  }
});

test("信息图：换焦点后画布内容随之改变，且不会残留上一个焦点的邻居", () => {
  const loginId = idOf("登录接口");
  const first = buildInformationFlow(MOCK_SESSION_GRAPH, WEB.id, null);
  const second = buildInformationFlow(MOCK_SESSION_GRAPH, loginId, null);

  expect(second.focusId).toBe(loginId);
  expect(second.nodes.length).not.toBe(first.nodes.length);
  for (const node of second.nodes) {
    if (node.id === loginId || node.id.startsWith("__fold_")) continue;
    const connected = INFORMATION.edges.some(
      (edge) =>
        (edge.from_id === loginId && edge.to_id === node.id) ||
        (edge.to_id === loginId && edge.from_id === node.id),
    );
    expect(connected, `${node.id} 与焦点不直接相连`).toBe(true);
  }
});

test("信息图：只有一侧有邻居时整体左移，不会让图偏在画布中间", () => {
  const built = buildInformationFlow(MOCK_SESSION_GRAPH, WEB.id, null);
  expect(built.inbound).toBe(0);
  expect(built.outbound).toBeGreaterThan(0);
  const minX = Math.min(...built.nodes.map((node) => node.position.x));
  expect(minX).toBeLessThan(60);
});

test("攻击图：默认焦点是第一个状态，自环单独挂在焦点上", () => {
  const built = buildAttackFlow(MOCK_SESSION_GRAPH, null, null);
  expect(built.focusId).toBe(STATE.id);

  const loops = ATTACK.edges.filter((edge) => edge.from_id === STATE.id && edge.to_id === STATE.id);
  expect(built.loops).toBe(loops.length);
  expect(loops.length).toBeGreaterThan(0);

  const marked = built.edges.filter((edge) => (edge.data as { loop?: boolean } | undefined)?.loop);
  expect(marked.length).toBe(loops.length);
});

test("攻击图：尝试的端点只能是攻击状态，且每条边都属于当前焦点", () => {
  const built = buildAttackFlow(MOCK_SESSION_GRAPH, null, null);
  const stateIds = new Set(ATTACK.nodes.map((node) => node.id));
  for (const edge of built.edges) {
    expect(stateIds.has(edge.source)).toBe(true);
    expect(stateIds.has(edge.target)).toBe(true);
    const touchesFocus = edge.source === built.focusId || edge.target === built.focusId;
    expect(touchesFocus, `${edge.id} 与当前焦点无关`).toBe(true);
  }
});

test("每种 outcome / status 都有对应的视觉档位", () => {
  const information = buildInformationFlow(MOCK_SESSION_GRAPH, null, null);
  for (const edge of information.edges) {
    const source = INFORMATION.edges.find((item) => item.id === edge.id);
    expect(edge.className).toContain(`edge-${source?.status}`);
  }
  const attack = buildAttackFlow(MOCK_SESSION_GRAPH, null, null);
  for (const edge of attack.edges) {
    const source = ATTACK.edges.find((item) => item.id === edge.id);
    expect(edge.className).toContain(`edge-${source?.outcome}`);
  }
});

test("同一张图里节点矩形互不重叠（含折叠节点）", () => {
  const boxOf = (built: ReturnType<typeof buildInformationFlow>) =>
    built.nodes.map((node) => {
      const width = Number(node.width ?? 200);
      const height = node.id.startsWith("__fold_") ? 64 : 96;
      return {
        id: node.id,
        left: node.position.x,
        right: node.position.x + width,
        top: node.position.y,
        bottom: node.position.y + height,
      };
    });

  for (const built of [
    buildInformationFlow(MOCK_SESSION_GRAPH, null, null),
    buildInformationFlow(MOCK_SESSION_GRAPH, idOf("用户管理接口"), null),
    buildAttackFlow(MOCK_SESSION_GRAPH, null, null),
  ]) {
    const items = boxOf(built);
    for (let i = 0; i < items.length; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        const a = items[i]!;
        const b = items[j]!;
        const overlapX = a.left < b.right && b.left < a.right;
        const overlapY = a.top < b.bottom && b.top < a.bottom;
        expect(overlapX && overlapY, `节点重叠：${a.id} / ${b.id}`).toBe(false);
      }
    }
  }
});

test("演示数据本身符合后端写入约束", () => {
  const entityIds = INFORMATION.nodes.map((node) => node.id);
  expect(new Set(entityIds).size).toBe(entityIds.length);

  for (const node of INFORMATION.nodes) {
    for (const ref of node.source_refs) {
      expect(parseSourceRef(ref).kind).not.toBe("unknown");
    }
  }
  for (const edge of INFORMATION.edges) {
    for (const ref of edge.source_refs) {
      expect(parseSourceRef(ref).kind).not.toBe("unknown");
    }
    expect(entityIds).toContain(edge.from_id);
    expect(entityIds).toContain(edge.to_id);
  }

  for (const state of ATTACK.nodes) {
    for (const ref of state.entity_refs) expect(entityIds).toContain(ref);
    expect(Object.keys(state).sort()).toEqual(["entity_refs", "id", "summary"]);
  }

  const stateIds = ATTACK.nodes.map((node) => node.id);
  for (const edge of ATTACK.edges) {
    expect(stateIds).toContain(edge.from_id);
    expect(stateIds).toContain(edge.to_id);
    expect(edge.source_refs.length).toBeGreaterThanOrEqual(1);
  }
});

test("演示数据规模足以验证「图会一直变大」", () => {
  expect(INFORMATION.nodes.length).toBeGreaterThanOrEqual(30);
  expect(INFORMATION.edges.length).toBeGreaterThanOrEqual(40);
  expect(ATTACK.nodes.length).toBeGreaterThanOrEqual(12);
  // 至少有一个枢纽实体的入边数超过单侧上限，折叠逻辑才有素材。
  const degrees = INFORMATION.nodes.map((node) => into(node.id).length);
  expect(Math.max(...degrees)).toBeGreaterThan(WING_LIMIT);
});
