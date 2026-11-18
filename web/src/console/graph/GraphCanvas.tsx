import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  Background, MiniMap, ReactFlow, useNodesInitialized, useReactFlow, useStore,
  type ReactFlowState, type Viewport,
} from "@xyflow/react";
import { GRAPH_NODE_TYPES } from "./GraphNodes";
import { GRAPH_EDGE_TYPES } from "./GraphEdges";
import { NODE_HEIGHT, NODE_WIDTH, type BuiltFlow, type FlowEdge, type FlowNode } from "./graphLayout";
import type { GraphKey } from "./graphSchema";
import { ATTEMPT_OUTCOME_LABELS, RELATION_STATUS_LABELS } from "./graphLabels";

const MAX_ZOOM = 2.5;
const selectZoom = (state: ReactFlowState) => state.transform[2];
const selectWidth = (state: ReactFlowState) => state.width;
const selectHeight = (state: ReactFlowState) => state.height;
// This flow is controlled. Check measured handles directly even when the
// library has not received a new nodes prop after its ResizeObserver fired.
const measurementOptions = { includeHiddenNodes: true };

function ViewControls({ onFit }: { onFit: () => void }) {
  const zoom = useStore(selectZoom);
  const { zoomIn, zoomOut } = useReactFlow();
  return (
    <div className="gtools" role="group" aria-label="画布视图">
      <button type="button" aria-label="缩小" title="缩小" onClick={() => void zoomOut()}>−</button>
      <output aria-label="缩放比例">{Math.round(zoom * 100)}%</output>
      <button type="button" aria-label="放大" title="放大" onClick={() => void zoomIn()}>+</button>
      <button type="button" className="gfit" title="适配全图" onClick={onFit}>
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true">
          <path d="M6 2H2v4m8-4h4v4M2 10v4h4m8-4v4h-4" strokeWidth="1.5" />
        </svg>
        全图
      </button>
    </div>
  );
}

function Legend({ graph }: { graph: GraphKey }) {
  const items = graph === "information" ? RELATION_STATUS_LABELS : ATTEMPT_OUTCOME_LABELS;
  return (
    <div className="glegend" aria-label="图例">
      {Object.entries(items).map(([key, label]) => (
        <span key={key}>
          <svg viewBox="0 0 22 8" width="22" height="8" aria-hidden="true">
            <path className={'gedge-line ' + key} d="M1 4h20" />
          </svg>
          {label}
        </span>
      ))}
    </div>
  );
}

export type LocateRequest = { id: string };

export function GraphCanvas({
  built, graph, nodeId, edgeId, locate, viewport, onViewport, onNode, onEdge, onLocated,
}: {
  built: BuiltFlow;
  graph: GraphKey;
  nodeId: string | null;
  edgeId: string | null;
  locate: LocateRequest | null;
  viewport: Viewport | undefined;
  onViewport: (viewport: Viewport) => void;
  onNode: (id: string) => void;
  onEdge: (id: string | null) => void;
  onLocated: (request: LocateRequest) => void;
}) {
  const { setViewport, getZoom, viewportInitialized } = useReactFlow<FlowNode, FlowEdge>();
  const nodesReady = useNodesInitialized(measurementOptions);
  const width = useStore(selectWidth);
  const height = useStore(selectHeight);
  const canvas = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);
  const ready = viewportInitialized && nodesReady && width > 0 && height > 0;
  const insetX = Math.min(26, width / 10);
  const insetTop = Math.min(54, height / 5);
  // Leave the controls, legend and minimap clear of the fitted graph.
  const insetBottom = Math.min(121, height / 4);
  const fitScale = Math.min((width - 2 * insetX) / built.bounds.width, (height - insetTop - insetBottom) / built.bounds.height);
  const minZoom = Math.min(0.16, Math.max(0.000001, fitScale * 0.7));
  const fit = useCallback(() => {
    if (!ready) return;
    const { x, y, width: graphWidth, height: graphHeight } = built.bounds;
    const zoom = Math.max(minZoom, Math.min(fitScale, 1.18));
    const fitted = {
      x: width / 2 - (x + graphWidth / 2) * zoom,
      y: (height + insetTop - insetBottom) / 2 - (y + graphHeight / 2) * zoom,
      zoom,
    };
    void setViewport(fitted);
    onViewport(fitted);
  }, [built.bounds, fitScale, height, insetBottom, insetTop, minZoom, onViewport, ready, setViewport, width]);

  useEffect(() => {
    if (!ready || initialized.current) return;
    initialized.current = true;
    if (!viewport) fit();
  }, [fit, ready, viewport]);

  useEffect(() => {
    if (!ready || !locate) return;
    const node = built.nodeById.get(locate.id);
    if (node && canvas.current) {
      const zoom = Math.max(getZoom(), 0.95);
      // Selecting a search result can open the detail panel in the same render.
      // Use the committed canvas size before React Flow's resize notification arrives.
      const next = {
        zoom,
        x: canvas.current.clientWidth / 2 - (node.position.x + NODE_WIDTH / 2) * zoom,
        y: canvas.current.clientHeight / 2 - (node.position.y + NODE_HEIGHT / 2) * zoom,
      };
      void setViewport(next);
      onViewport(next);
    }
    onLocated(locate);
  }, [built.nodeById, getZoom, locate, onLocated, onViewport, ready, setViewport]);

  const nodes = useMemo(() => {
    const edge = edgeId ? built.edgeById.get(edgeId) : undefined;
    const related = new Set(edge ? [edge.source, edge.target] : []);
    return built.nodes.map(node => node.id === nodeId || related.has(node.id)
      ? { ...node, selected: node.id === nodeId && !edge, data: { ...node.data, related: related.has(node.id) } }
      : node);
  }, [built, edgeId, nodeId]);
  const edges = useMemo(() => {
    const adjacent = new Set(nodeId && !edgeId ? built.incident.get(nodeId) : []);
    return built.edges.map(edge => {
      const selected = edge.id === edgeId;
      const related = adjacent.has(edge.id);
      return selected || related ? { ...edge, selected, zIndex: selected ? 3 : 1, data: { ...edge.data!, related } } : edge;
    });
  }, [built, edgeId, nodeId]);

  return (
    <div ref={canvas} className="gcanvas" aria-label={graph === "information" ? "信息图画布" : "攻击图画布"}>
      <ReactFlow<FlowNode, FlowEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={GRAPH_NODE_TYPES}
        edgeTypes={GRAPH_EDGE_TYPES}
        defaultViewport={viewport}
        minZoom={minZoom}
        maxZoom={MAX_ZOOM}
        onMoveEnd={(_, next) => onViewport(next)}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesReconnectable={false}
        deleteKeyCode={null}
        onNodeClick={(_, node) => onNode(node.id)}
        onEdgeClick={(_, edge) => onEdge(edge.id)}
        onPaneClick={() => onEdge(null)}
        onKeyDown={event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          const element = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node, .react-flow__edge");
          const id = element?.dataset.id;
          if (!id) return;
          if (element.classList.contains("react-flow__node")) onNode(id);
          else onEdge(id);
        }}
        className="gflow"
        data-ready={ready}
        data-dense={built.edges.length > 30}
      >
        <Background color="#dedee4" gap={18} size={1} />
        <ViewControls onFit={fit} />
        <MiniMap
          position="bottom-right"
          ariaLabel="全图缩略导航"
          nodeColor={node => node.selected ? "#f4512c" : "#a1a1aa"}
          nodeStrokeWidth={0}
          nodeBorderRadius={8}
          maskColor="rgba(82,82,91,.10)"
          maskStrokeColor="#c33a1d"
          maskStrokeWidth={1}
          pannable
          zoomable
        />
      </ReactFlow>
      <Legend graph={graph} />
    </div>
  );
}
