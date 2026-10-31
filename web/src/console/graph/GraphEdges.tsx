/**
 * 双图的自定义连线。
 *
 * 线型与颜色只表达后端真实枚举：
 * - 信息图边用 `Relation.status`（observed / hypothesis / refuted / superseded）。
 * - 攻击图边用 `Attempt.outcome`（success / negative / blocked / unknown）。
 * 自环（`from_id === to_id`）单独画成节点上方的一段回环，表示「在同一状态上又试了一次」。
 */

import {
  BaseEdge,
  EdgeLabelRenderer,
  Position,
  getSmoothStepPath,
  useInternalNode,
  type Edge,
  type EdgeProps,
} from "@xyflow/react";
import {
  ATTEMPT_OUTCOME_TONES,
  RELATION_STATUS_TONES,
  TONE_COLORS,
  statusLabel,
  outcomeLabel,
} from "./graphLabels";
import type { AttemptEdgeData, RelationEdgeData } from "./graphLayout";

const STATE_NODE_WIDTH = 240;
const STATE_NODE_HEIGHT = 96;

/** 关系名大多长于 2 个字，标签里截断，完整文本在详情面板。 */
function trim(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/**
 * 自绘箭头。不依赖 SVG <marker>：React Flow 生成的 marker id 里带 `#`，
 * 在 url(#...) 里会被当成片段分隔符，实际不渲染。
 * smoothstep 的最后一段总是沿 handle 方向直线进入，所以按 position 画三角形就够精确。
 *
 * `position` 是「从哪一侧进入目标节点」，箭头要指向节点内部：
 * 从左侧进入 → 朝右；从顶部进入 → 朝下。
 */
function ArrowHead({
  x,
  y,
  position,
  color,
}: {
  x: number;
  y: number;
  position: Position;
  color: string;
}) {
  const size = 11;
  const half = size * 0.55;
  const points =
    position === Position.Left
      ? `${x},${y} ${x - size},${y - half} ${x - size},${y + half}`
      : position === Position.Right
        ? `${x},${y} ${x + size},${y - half} ${x + size},${y + half}`
        : position === Position.Top
          ? `${x},${y} ${x - half},${y - size} ${x + half},${y - size}`
          : `${x},${y} ${x - half},${y + size} ${x + half},${y + size}`;
  return <polygon points={points} fill={color} className="garrow" />;
}

const RELATION_LABEL_MAX = 16;
const ATTEMPT_LABEL_MAX = 20;
const LOOP_LABEL_MAX = 18;

export function RelationEdge(props: EdgeProps<Edge<RelationEdgeData>>) {
  const { sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected } = props;
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 16,
    offset: 18,
  });
  const tone = data ? RELATION_STATUS_TONES[data.edge.status] ?? "superseded" : "superseded";
  return (
    <>
      <BaseEdge
        path={path}
        className={`gedge-line ${tone}${selected ? " on" : ""}`}
        interactionWidth={22}
      />
      <ArrowHead
        x={targetX}
        y={targetY}
        position={targetPosition}
        color={TONE_COLORS[tone] ?? "#8a8a92"}
      />
      {data && (
        <EdgeLabelRenderer>
          <div
            className={`gedge-label ${tone}${selected ? " on" : ""}${data.dimmed ? " dim" : ""}`}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY - 9}px)` }}
          >
            <span className={`gdot ${tone}`} aria-hidden="true" />
            <span className="gedge-text">{trim(data.edge.relation, RELATION_LABEL_MAX)}</span>
            <span className="gedge-raw">{statusLabel(data.edge.status)}</span>
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export function AttemptEdge(props: EdgeProps<Edge<AttemptEdgeData>>) {
  const { source, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected } =
    props;
  const node = useInternalNode(source);
  const tone = data ? ATTEMPT_OUTCOME_TONES[data.edge.outcome] ?? "unknown" : "unknown";

  if (data?.loop) {
    const width = node?.measured?.width ?? STATE_NODE_WIDTH;
    const height = node?.measured?.height ?? STATE_NODE_HEIGHT;
    const originX = node?.internals.positionAbsolute.x ?? sourceX - width / 2;
    const originY = node?.internals.positionAbsolute.y ?? sourceY - height;
    // 焦点固定在中间，自环统一挂在节点下方，不会压到左右两列的连线。
    const startX = originX + width - 34;
    const endX = originX + width * 0.54;
    const edgeY = originY + height;
    const lift = 54;
    const bowY = edgeY + lift;
    const path = `M ${startX} ${edgeY} C ${startX + 30} ${bowY}, ${endX - 30} ${bowY}, ${endX} ${edgeY}`;
    const labelY = edgeY + lift + 11;
    return (
      <>
        <BaseEdge
          path={path}
          className={`gedge-line loop ${tone}${selected ? " on" : ""}`}
          interactionWidth={20}
        />
        <ArrowHead x={endX} y={edgeY} position={Position.Bottom} color={TONE_COLORS[tone] ?? "#8a8a92"} />
        {data && (
          <EdgeLabelRenderer>
            <div
              className={`gedge-label loop ${tone}${selected ? " on" : ""}${data.dimmed ? " dim" : ""}`}
              style={{
                transform: `translate(-50%, -50%) translate(${(startX + endX) / 2}px, ${labelY}px)`,
              }}
            >
              <span className={`gdot ${tone}`} aria-hidden="true" />
              <span className="gedge-text">{trim(data.edge.action, LOOP_LABEL_MAX)}</span>
              <span className="gedge-raw">自环</span>
            </div>
          </EdgeLabelRenderer>
        )}
      </>
    );
  }

  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 16,
    offset: 18,
  });
  return (
    <>
      <BaseEdge
        path={path}
        className={`gedge-line ${tone}${selected ? " on" : ""}`}
        interactionWidth={22}
      />
      <ArrowHead
        x={targetX}
        y={targetY}
        position={targetPosition}
        color={TONE_COLORS[tone] ?? "#8a8a92"}
      />
      {data && (
        <EdgeLabelRenderer>
          <div
            className={`gedge-label attempt ${tone}${selected ? " on" : ""}${data.dimmed ? " dim" : ""}`}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY - 9}px)` }}
          >
            <span className={`gdot ${tone}`} aria-hidden="true" />
            <span className="gedge-text">{trim(data.edge.action, ATTEMPT_LABEL_MAX)}</span>
            <span className="gedge-raw">{outcomeLabel(data.edge.outcome)}</span>
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const GRAPH_EDGE_TYPES = {
  relation: RelationEdge,
  attempt: AttemptEdge,
};
