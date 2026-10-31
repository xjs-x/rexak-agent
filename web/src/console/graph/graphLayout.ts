/**
 * 双图 → React Flow 的视图模型构造。
 *
 * 这里只做「位置、尺寸、视觉档位」的派生，不产生任何新的业务语义：
 * 每个节点 / 每条连线都能追溯到 `SessionGraphDocument` 里的一个对象。
 *
 * 布局形式是「焦点 + 入/出两翼」：
 *
 *   入边来源       焦点        出边目标
 *   [实体 A]  ──→  [实体]  ──→  [实体 C]
 *   [实体 B]  ──→          ──→  [实体 D]
 *
 * 也就是把当前焦点对象放在中间，`to_id === 焦点` 的放左边，
 * `from_id === 焦点` 的放右边；自环单独挂在焦点下方。
 *
 * 每一翼最多画 WING_LIMIT 个，多出来的收进一个「还有 N 个」的折叠节点。
 * 这样单个枢纽实体（例如被 9 个对象指向的接口）不会把一列撑到几千像素，
 * 也就不会把整张图缩到看不清。折叠掉的对象仍然可以在详情面板里全部列出、点选。
 */

import { Position, type Edge, type Node } from "@xyflow/react";
import type {
  AttemptEdge,
  AttemptOutcome,
  InformationNode,
  RelationEdge,
  SessionGraphDocument,
} from "./graphSchema";
import { ATTEMPT_OUTCOME_TONES, RELATION_STATUS_TONES } from "./graphLabels";

export const NODE_WIDTH = 210;
export const STATE_WIDTH = 220;
const NODE_HEIGHT = 88;
const STATE_HEIGHT = 92;
const FOLD_WIDTH = 210;
const FOLD_HEIGHT = 64;

/** 相邻两行的中心距。要留得下一行边标签，否则标签会压到下一行节点。 */
const ROW_GAP = 118;
/** 相邻两列的中心距。节点宽 210 / 220，留 110 给连线与标签。 */
const COLUMN_GAP = 340;
/** 单侧最多画几个。超过就折成「还有 N 个」。 */
export const WING_LIMIT = 6;

/** 选中目标：只用来高亮某条连线，不改变焦点。 */
export type Selection = { kind: "edge"; graph: "information" | "attack"; id: string } | null;

export type NodeRole = "focus" | "inbound" | "outbound";

/** 折叠节点：这一侧还有多少个没画出来。 */
export type FoldData = {
  side: "inbound" | "outbound";
  hidden: number;
  total: number;
} & Record<string, unknown>;

export type EntityNodeData = {
  entity: InformationNode;
  relationCount: number;
  evidenceCount: number;
  role: NodeRole;
  dimmed: boolean;
} & Record<string, unknown>;

export type AttackStateNodeData = {
  state: { id: string; summary: string; entity_refs: string[] };
  entityLabels: string[];
  outcomeCounts: Partial<Record<AttemptOutcome, number>>;
  attemptCount: number;
  role: NodeRole;
  dimmed: boolean;
} & Record<string, unknown>;

export type RelationEdgeData = {
  edge: RelationEdge;
  dimmed: boolean;
  active: boolean;
  /** `inbound` 表示箭头指向焦点，`outbound` 表示从焦点指出。 */
  direction: "inbound" | "outbound" | "loop";
} & Record<string, unknown>;

export type AttemptEdgeData = {
  edge: AttemptEdge;
  loop: boolean;
  loopDown: boolean;
  dimmed: boolean;
  active: boolean;
  direction: "inbound" | "outbound" | "loop";
} & Record<string, unknown>;

/** 被折叠掉的对象，供详情面板列全量清单。 */
export type HiddenItem = { id: string; name: string };

export type BuiltFlow = {
  nodes: Node[];
  edges: Edge[];
  focusId: string | null;
  inbound: number;
  outbound: number;
  loops: number;
  /** 被折叠的对象，按侧分组。 */
  hidden: { inbound: HiddenItem[]; outbound: HiddenItem[] };
  /** 引用了不存在节点的边数量。后端写入时会校验，读取历史文档时仍要如实呈现异常。 */
  dangling: number;
};

type Handles = {
  sourceHandle: string;
  targetHandle: string;
  sourcePosition: Position;
  targetPosition: Position;
};

/** 左列 → 焦点：从左侧节点右边出，进焦点左边。 */
const INBOUND_HANDLES: Handles = {
  sourceHandle: "r-src",
  targetHandle: "l-tgt",
  sourcePosition: Position.Right,
  targetPosition: Position.Left,
};

/** 焦点 → 右列：从焦点右边出，进右侧节点左边。 */
const OUTBOUND_HANDLES: Handles = {
  sourceHandle: "r-src",
  targetHandle: "l-tgt",
  sourcePosition: Position.Right,
  targetPosition: Position.Left,
};

/** 自环：从焦点底部出，绕一段回到底部，落在焦点下方。 */
const LOOP_HANDLES: Handles = {
  sourceHandle: "b-src",
  targetHandle: "b-tgt",
  sourcePosition: Position.Bottom,
  targetPosition: Position.Bottom,
};

function columnY(count: number, index: number, centerY: number): number {
  return centerY + (index - (count - 1) / 2) * ROW_GAP;
}

/** 三列共用的纵向基准：按左右两列里较长的那个撑开，焦点始终居中。 */
function columnCenter(rows: number): number {
  const height = Math.max(rows, 1) * ROW_GAP;
  return 80 + height / 2;
}

const LEFT_X = 40 + NODE_WIDTH / 2;
const MIDDLE_X = LEFT_X + COLUMN_GAP;
const RIGHT_X = MIDDLE_X + COLUMN_GAP;

/**
 * 三列的横坐标。某一翼为空时整体左移，避免出现「只有右边有东西、
 * 整张图却偏在画布右半侧」的失衡。
 */
function columnXs(hasInbound: boolean, hasOutbound: boolean) {
  const middle = hasInbound ? MIDDLE_X : LEFT_X;
  const right = hasInbound && hasOutbound ? RIGHT_X : MIDDLE_X;
  return { left: LEFT_X, middle, right };
}

function nodePosition(center: { x: number; y: number }, width: number, height: number) {
  return { x: center.x - width / 2, y: center.y - height / 2 };
}

/**
 * 焦点为空（例如文档里没有任何节点）时返回空图；
 * 焦点 ID 失效时退回第一个对象，保证界面永远有东西可看。
 */
function resolveFocus<T extends { id: string }>(items: T[], focusId: string | null): T | null {
  if (!items.length) return null;
  return items.find((item) => item.id === focusId) ?? items[0] ?? null;
}

function emptyFlow(): BuiltFlow {
  return {
    nodes: [],
    edges: [],
    focusId: null,
    inbound: 0,
    outbound: 0,
    loops: 0,
    hidden: { inbound: [], outbound: [] },
    dangling: 0,
  };
}

/** 折叠节点的 ID 固定，方便点击时识别，也不会和真实对象 ID 冲突。 */
export const FOLD_ID = {
  inbound: "__fold_inbound__",
  outbound: "__fold_outbound__",
} as const;

function pushFoldNode(
  nodes: Node[],
  side: "inbound" | "outbound",
  hidden: number,
  total: number,
  center: { x: number; y: number },
) {
  nodes.push({
    id: FOLD_ID[side],
    type: "fold",
    position: nodePosition(center, FOLD_WIDTH, FOLD_HEIGHT),
    width: FOLD_WIDTH,
    data: { side, hidden, total } satisfies FoldData,
    draggable: false,
    selectable: false,
  });
}

export function buildInformationFlow(
  document: SessionGraphDocument,
  focusId: string | null,
  selection: Selection,
): BuiltFlow {
  const source = document.information_graph;
  const focus = resolveFocus(source.nodes, focusId);
  if (!focus) return emptyFlow();

  const byId = new Map(source.nodes.map((item) => [item.id, item]));
  const inboundAll = source.edges.filter(
    (edge) => edge.to_id === focus.id && edge.from_id !== focus.id,
  );
  const outboundAll = source.edges.filter(
    (edge) => edge.from_id === focus.id && edge.to_id !== focus.id,
  );
  const loopEdges = source.edges.filter(
    (edge) => edge.from_id === focus.id && edge.to_id === focus.id,
  );

  const inboundEdges = inboundAll.slice(0, WING_LIMIT);
  const outboundEdges = outboundAll.slice(0, WING_LIMIT);
  const foldedIn = inboundAll.slice(WING_LIMIT);
  const foldedOut = outboundAll.slice(WING_LIMIT);

  const inboundRows = inboundEdges.length + (foldedIn.length > 0 ? 1 : 0);
  const outboundRows = outboundEdges.length + (foldedOut.length > 0 ? 1 : 0);
  const rows = Math.max(inboundRows, outboundRows, 1);
  const centerY = columnCenter(rows);
  const xs = columnXs(inboundRows > 0, outboundRows > 0);

  const nodes: Node[] = [];
  const edges: Edge[] = [];
  let dangling = 0;

  const relationCountOf = (id: string) =>
    source.edges.filter((edge) => edge.from_id === id || edge.to_id === id).length;

  const pushNode = (entity: InformationNode, role: NodeRole, center: { x: number; y: number }) => {
    const data: EntityNodeData = {
      entity,
      relationCount: relationCountOf(entity.id),
      evidenceCount: entity.source_refs.length,
      role,
      dimmed: false,
    };
    nodes.push({
      id: entity.id,
      type: "entity",
      position: nodePosition(center, NODE_WIDTH, NODE_HEIGHT),
      width: NODE_WIDTH,
      data,
      draggable: false,
    });
  };

  // 左列：指向焦点的实体。
  inboundEdges.forEach((edge, index) => {
    const other = byId.get(edge.from_id);
    if (!other) {
      dangling += 1;
      return;
    }
    pushNode(other, "inbound", { x: xs.left, y: columnY(inboundRows, index, centerY) });
  });
  if (foldedIn.length) {
    pushFoldNode(nodes, "inbound", foldedIn.length, inboundAll.length, {
      x: xs.left,
      y: columnY(inboundRows, inboundEdges.length, centerY),
    });
  }

  // 右列：焦点指向的实体。
  outboundEdges.forEach((edge, index) => {
    const other = byId.get(edge.to_id);
    if (!other) {
      dangling += 1;
      return;
    }
    pushNode(other, "outbound", { x: xs.right, y: columnY(outboundRows, index, centerY) });
  });
  if (foldedOut.length) {
    pushFoldNode(nodes, "outbound", foldedOut.length, outboundAll.length, {
      x: xs.right,
      y: columnY(outboundRows, outboundEdges.length, centerY),
    });
  }

  // 中间：焦点。
  pushNode(focus, "focus", { x: xs.middle, y: centerY });

  const pushEdge = (
    edge: RelationEdge,
    direction: "inbound" | "outbound" | "loop",
    handles: Handles,
  ) => {
    const active = selection?.id === edge.id;
    edges.push({
      id: edge.id,
      source: edge.from_id,
      target: edge.to_id,
      type: "relation",
      ...handles,
      data: { edge, active, dimmed: false, direction } satisfies RelationEdgeData,
      selected: active,
      zIndex: active ? 3 : 1,
      className: `edge-relation edge-${RELATION_STATUS_TONES[edge.status] ?? "superseded"}${
        active ? " on" : ""
      }`,
    });
  };

  inboundEdges.forEach((edge) => {
    if (byId.has(edge.from_id)) pushEdge(edge, "inbound", INBOUND_HANDLES);
  });
  outboundEdges.forEach((edge) => {
    if (byId.has(edge.to_id)) pushEdge(edge, "outbound", OUTBOUND_HANDLES);
  });
  loopEdges.forEach((edge) => pushEdge(edge, "loop", LOOP_HANDLES));

  return {
    nodes,
    edges,
    focusId: focus.id,
    inbound: inboundAll.length,
    outbound: outboundAll.length,
    loops: loopEdges.length,
    hidden: {
      inbound: foldedIn
        .map((edge) => byId.get(edge.from_id))
        .filter((item): item is InformationNode => Boolean(item))
        .map((item) => ({ id: item.id, name: item.label })),
      outbound: foldedOut
        .map((edge) => byId.get(edge.to_id))
        .filter((item): item is InformationNode => Boolean(item))
        .map((item) => ({ id: item.id, name: item.label })),
    },
    dangling,
  };
}

export function buildAttackFlow(
  document: SessionGraphDocument,
  focusId: string | null,
  selection: Selection,
): BuiltFlow {
  const source = document.attack_graph;
  const focus = resolveFocus(source.nodes, focusId);
  if (!focus) return emptyFlow();

  const byId = new Map(source.nodes.map((item) => [item.id, item]));
  const entityById = new Map(document.information_graph.nodes.map((item) => [item.id, item]));

  const inboundAll = source.edges.filter(
    (edge) => edge.to_id === focus.id && edge.from_id !== focus.id,
  );
  const outboundAll = source.edges.filter(
    (edge) => edge.from_id === focus.id && edge.to_id !== focus.id,
  );
  const loopEdges = source.edges.filter(
    (edge) => edge.from_id === focus.id && edge.to_id === focus.id,
  );

  const inboundEdges = inboundAll.slice(0, WING_LIMIT);
  const outboundEdges = outboundAll.slice(0, WING_LIMIT);
  const foldedIn = inboundAll.slice(WING_LIMIT);
  const foldedOut = outboundAll.slice(WING_LIMIT);

  const inboundRows = inboundEdges.length + (foldedIn.length > 0 ? 1 : 0);
  const outboundRows = outboundEdges.length + (foldedOut.length > 0 ? 1 : 0);
  const rows = Math.max(inboundRows, outboundRows, 1);
  const centerY = columnCenter(rows);
  const xs = columnXs(inboundRows > 0, outboundRows > 0);

  const nodes: Node[] = [];
  const edges: Edge[] = [];
  let dangling = 0;

  const pushNode = (
    state: { id: string; summary: string; entity_refs: string[] },
    role: NodeRole,
    center: { x: number; y: number },
  ) => {
    const touches = source.edges.filter(
      (edge) => edge.from_id === state.id || edge.to_id === state.id,
    );
    const outcomeCounts: Partial<Record<AttemptOutcome, number>> = {};
    for (const edge of touches) {
      outcomeCounts[edge.outcome] = (outcomeCounts[edge.outcome] ?? 0) + 1;
    }
    const entityLabels = state.entity_refs
      .map((ref) => entityById.get(ref)?.label)
      .filter((label): label is string => Boolean(label));
    const data: AttackStateNodeData = {
      state,
      entityLabels,
      outcomeCounts,
      attemptCount: touches.length,
      role,
      dimmed: false,
    };
    nodes.push({
      id: state.id,
      type: "attackState",
      position: nodePosition(center, STATE_WIDTH, STATE_HEIGHT),
      width: STATE_WIDTH,
      data,
      draggable: false,
    });
  };

  inboundEdges.forEach((edge, index) => {
    const other = byId.get(edge.from_id);
    if (!other) {
      dangling += 1;
      return;
    }
    pushNode(other, "inbound", { x: xs.left, y: columnY(inboundRows, index, centerY) });
  });
  if (foldedIn.length) {
    pushFoldNode(nodes, "inbound", foldedIn.length, inboundAll.length, {
      x: xs.left,
      y: columnY(inboundRows, inboundEdges.length, centerY),
    });
  }

  outboundEdges.forEach((edge, index) => {
    const other = byId.get(edge.to_id);
    if (!other) {
      dangling += 1;
      return;
    }
    pushNode(other, "outbound", { x: xs.right, y: columnY(outboundRows, index, centerY) });
  });
  if (foldedOut.length) {
    pushFoldNode(nodes, "outbound", foldedOut.length, outboundAll.length, {
      x: xs.right,
      y: columnY(outboundRows, outboundEdges.length, centerY),
    });
  }

  pushNode(focus, "focus", { x: xs.middle, y: centerY });

  const pushEdge = (
    edge: AttemptEdge,
    direction: "inbound" | "outbound" | "loop",
    handles: Handles,
  ) => {
    const active = selection?.id === edge.id;
    const loop = direction === "loop";
    edges.push({
      id: edge.id,
      source: edge.from_id,
      target: edge.to_id,
      type: "attempt",
      ...handles,
      data: {
        edge,
        loop,
        loopDown: true,
        active,
        dimmed: false,
        direction,
      } satisfies AttemptEdgeData,
      selected: active,
      zIndex: active ? 3 : 1,
      className: `edge-attempt edge-${ATTEMPT_OUTCOME_TONES[edge.outcome] ?? "unknown"}${
        active ? " on" : ""
      }`,
    });
  };

  inboundEdges.forEach((edge) => {
    if (byId.has(edge.from_id)) pushEdge(edge, "inbound", INBOUND_HANDLES);
  });
  outboundEdges.forEach((edge) => {
    if (byId.has(edge.to_id)) pushEdge(edge, "outbound", OUTBOUND_HANDLES);
  });
  loopEdges.forEach((edge) => pushEdge(edge, "loop", LOOP_HANDLES));

  return {
    nodes,
    edges,
    focusId: focus.id,
    inbound: inboundAll.length,
    outbound: outboundAll.length,
    loops: loopEdges.length,
    hidden: {
      inbound: foldedIn
        .map((edge) => byId.get(edge.from_id))
        .filter((item): item is (typeof source.nodes)[number] => Boolean(item))
        .map((item) => ({ id: item.id, name: item.summary })),
      outbound: foldedOut
        .map((edge) => byId.get(edge.to_id))
        .filter((item): item is (typeof source.nodes)[number] => Boolean(item))
        .map((item) => ({ id: item.id, name: item.summary })),
    },
    dangling,
  };
}
