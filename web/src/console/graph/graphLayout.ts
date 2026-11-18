/** Stable full-document layout for the read-only Session graph. */
import type { Edge, Node, Rect, XYPosition } from "@xyflow/react";
import type {
  AttackNode, AttemptEdge, GraphKey, InformationNode, RelationEdge, SessionGraphDocument,
} from "./graphSchema";

export const NODE_WIDTH = 148;
export const NODE_HEIGHT = 54;
const X_GAP = 218;
const Y_GAP = 134;
const COMPONENT_GAP = 96;

type GraphNode = InformationNode | AttackNode;
type GraphEdge = RelationEdge | AttemptEdge;
type NodeData = {
  object: GraphNode;
  name: string;
  degree: number;
  related?: boolean;
};
type EdgeData = {
  object: GraphEdge;
  path: string;
  arrow: string;
  labelAt: XYPosition;
  related?: boolean;
};
export type FlowNode = Node<NodeData, "sessionNode">;
export type FlowEdge = Edge<EdgeData, "sessionEdge">;
export type BuiltFlow = {
  nodes: FlowNode[];
  edges: FlowEdge[];
  nodeById: Map<string, FlowNode>;
  edgeById: Map<string, FlowEdge>;
  incident: Map<string, string[]>;
  bounds: Rect;
  components: number;
  invalidEdges: number;
  duplicateNodes: number;
  duplicateEdges: number;
};

const compareIds = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;

type Component = { ids: string[]; positions: Map<string, XYPosition>; width: number; height: number };

function positionComponent(ids: string[], neighbors: Map<string, Set<string>>): Component {
  const root = ids.reduce((best, id) =>
    neighbors.get(id)!.size > neighbors.get(best)!.size ? id : best,
  );
  const order = [root];
  const depth = new Map([[root, 0]]);
  const layers: string[][] = [];
  for (const id of order) {
    const level = depth.get(id)!;
    (layers[level] ??= []).push(id);
    for (const next of neighbors.get(id)!) {
      if (depth.has(next)) continue;
      depth.set(next, level + 1);
      order.push(next);
    }
  }

  const positions = new Map<string, XYPosition>();
  if (layers.length <= 4 && ids.length <= 48) {
    // Expand each ring until it can hold its row of cards without collisions.
    let radius = 0;
    layers.forEach((layer, level) => {
      if (level === 0) {
        positions.set(root, { x: 0, y: 0 });
        return;
      }
      radius = Math.max(radius + 184, layer.length > 1 ? 111 / Math.sin(Math.PI / layer.length) : 0);
      layer.forEach((id, index) => {
        const angle = -Math.PI / 2 + 2 * Math.PI * index / layer.length;
        positions.set(id, { x: Math.round(radius * Math.cos(angle)), y: Math.round(radius * Math.sin(angle)) });
      });
    });
  } else {
    // Fold long or wide components into a compact serpentine grid in breadth-first order.
    const columns = Math.ceil(Math.sqrt(order.length * Y_GAP / X_GAP));
    order.forEach((id, index) => {
      const row = Math.floor(index / columns);
      const column = index % columns;
      positions.set(id, { x: (row % 2 ? columns - 1 - column : column) * X_GAP, y: row * Y_GAP });
    });
  }

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const { x, y } of positions.values()) {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }
  for (const point of positions.values()) {
    point.x -= minX;
    point.y -= minY;
  }
  return { ids, positions, width: maxX - minX + NODE_WIDTH, height: maxY - minY + NODE_HEIGHT };
}

function packComponents(ids: string[], neighbors: Map<string, Set<string>>) {
  const visited = new Set<string>();
  const components: Component[] = [];
  for (const id of ids) {
    if (visited.has(id)) continue;
    const members = [id];
    visited.add(id);
    for (const current of members) {
      for (const next of neighbors.get(current)!) {
        if (visited.has(next)) continue;
        visited.add(next);
        members.push(next);
      }
    }
    members.sort(compareIds);
    components.push(positionComponent(members, neighbors));
  }
  components.sort((a, b) => b.ids.length - a.ids.length || compareIds(a.ids[0]!, b.ids[0]!));

  const area = components.reduce((total, c) => total + (c.width + COMPONENT_GAP) * (c.height + COMPONENT_GAP), 0);
  const shelfWidth = components.reduce((width, c) => Math.max(width, c.width), Math.sqrt(area * 1.5));
  const positions = new Map<string, XYPosition>();
  let x = 0;
  let y = 0;
  let shelfHeight = 0;
  for (const component of components) {
    if (x > 0 && x + component.width > shelfWidth) {
      y += shelfHeight + COMPONENT_GAP;
      x = 0;
      shelfHeight = 0;
    }
    for (const [id, point] of component.positions) positions.set(id, { x: x + point.x, y: y + point.y });
    x += component.width + COMPONENT_GAP;
    shelfHeight = Math.max(shelfHeight, component.height);
  }
  return { positions, count: components.length };
}

function cardBorder(center: XYPosition, toward: XYPosition): XYPosition {
  const dx = toward.x - center.x;
  const dy = toward.y - center.y;
  const scale = 1 / Math.max(Math.abs(dx) / (NODE_WIDTH / 2), Math.abs(dy) / (NODE_HEIGHT / 2), 0.001);
  return { x: center.x + dx * scale, y: center.y + dy * scale };
}

function routeEdge(edge: GraphEdge, from: XYPosition, to: XYPosition, index: number, count: number) {
  const center = (p: XYPosition) => ({ x: p.x + NODE_WIDTH / 2, y: p.y + NODE_HEIGHT / 2 });
  const a = center(from);
  const b = center(to);
  if (edge.from_id === edge.to_id) {
    const extent = 24 + index * 15;
    return {
      start: { x: a.x - 19, y: from.y }, end: { x: a.x + 19, y: from.y },
      c1: { x: a.x - 32 - extent, y: from.y - extent },
      c2: { x: a.x + 32 + extent, y: from.y - extent },
    };
  }

  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.hypot(dx, dy);
  const sign = compareIds(edge.from_id, edge.to_id) < 0 ? 1 : -1;
  const lane = (index - (count - 1) / 2) * 22 * sign;
  const shift = { x: -dy / length * lane, y: dx / length * lane };
  const c1 = { x: a.x + dx * 0.38 + shift.x, y: a.y + dy * 0.38 + shift.y };
  const c2 = { x: b.x - dx * 0.38 + shift.x, y: b.y - dy * 0.38 + shift.y };
  return { start: cardBorder(a, c1), end: cardBorder(b, c2), c1, c2 };
}

export function buildGraphFlow(document: SessionGraphDocument, graph: GraphKey): BuiltFlow {
  const slice = graph === "information" ? document.information_graph : document.attack_graph;
  const objects = new Map<string, GraphNode>();
  for (const node of slice.nodes) if (!objects.has(node.id)) objects.set(node.id, node);
  const ids = [...objects.keys()].sort(compareIds);
  const neighbors = new Map(ids.map(id => [id, new Set<string>()]));
  const incident = new Map(ids.map(id => [id, [] as string[]]));
  const bundles = new Map<string, GraphEdge[]>();
  const seen = new Set<string>();
  let invalidEdges = 0;
  let duplicateEdges = 0;
  for (const edge of [...slice.edges].sort((a, b) => compareIds(a.id, b.id))) {
    if (!objects.has(edge.from_id) || !objects.has(edge.to_id)) {
      invalidEdges += 1;
      continue;
    }
    if (seen.has(edge.id)) {
      duplicateEdges += 1;
      continue;
    }
    seen.add(edge.id);
    incident.get(edge.from_id)!.push(edge.id);
    if (edge.from_id !== edge.to_id) {
      incident.get(edge.to_id)!.push(edge.id);
      neighbors.get(edge.from_id)!.add(edge.to_id);
      neighbors.get(edge.to_id)!.add(edge.from_id);
    }
    const key = JSON.stringify([edge.from_id, edge.to_id].sort(compareIds));
    const bundle = bundles.get(key);
    if (bundle) bundle.push(edge);
    else bundles.set(key, [edge]);
  }

  const { positions, count: components } = packComponents(ids, neighbors);
  const nodes: FlowNode[] = ids.map(id => {
    const object = objects.get(id)!;
    const name = "label" in object ? object.label : object.summary;
    return {
      id, type: "sessionNode", position: positions.get(id)!, width: NODE_WIDTH, height: NODE_HEIGHT,
      ariaLabel: name, draggable: false, data: { object, name, degree: incident.get(id)!.length },
    };
  });

  let minX = 0;
  let minY = 0;
  let maxX = 1;
  let maxY = 1;
  for (const { position } of nodes) {
    maxX = Math.max(maxX, position.x + NODE_WIDTH);
    maxY = Math.max(maxY, position.y + NODE_HEIGHT);
  }

  const edges: FlowEdge[] = [];
  for (const bundle of bundles.values()) {
    bundle.forEach((object, index) => {
      const { start, end, c1, c2 } = routeEdge(object, positions.get(object.from_id)!, positions.get(object.to_id)!, index, bundle.length);
      const labelAt = {
        x: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8,
        y: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8 - 6,
      };
      const angle = Math.atan2(end.y - c2.y, end.x - c2.x);
      const back = { x: end.x - 8 * Math.cos(angle), y: end.y - 8 * Math.sin(angle) };
      const side = { x: 3.5 * Math.sin(angle), y: -3.5 * Math.cos(angle) };
      minX = Math.min(minX, c1.x, c2.x, labelAt.x - 110);
      minY = Math.min(minY, c1.y, c2.y, labelAt.y - 13);
      maxX = Math.max(maxX, c1.x, c2.x, labelAt.x + 110);
      maxY = Math.max(maxY, c1.y, c2.y, labelAt.y + 4);
      edges.push({
        id: object.id, type: "sessionEdge", source: object.from_id, target: object.to_id,
        ariaLabel: "relation" in object ? object.relation : object.action, reconnectable: false,
        data: {
          object, labelAt,
          path: ['M', start.x, start.y, 'C', c1.x, c1.y, c2.x, c2.y, end.x, end.y].join(' '),
          arrow: [[end.x, end.y], [back.x + side.x, back.y + side.y], [back.x - side.x, back.y - side.y]].map(p => p.join(",")).join(" "),
        },
      });
    });
  }

  return {
    nodes, edges, incident, components, invalidEdges, duplicateEdges,
    duplicateNodes: slice.nodes.length - nodes.length,
    nodeById: new Map(nodes.map(node => [node.id, node])),
    edgeById: new Map(edges.map(edge => [edge.id, edge])),
    bounds: { x: minX, y: minY, width: maxX - minX, height: maxY - minY },
  };
}
