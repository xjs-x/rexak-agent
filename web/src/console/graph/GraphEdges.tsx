import { memo } from "react";
import { BaseEdge, type EdgeProps } from "@xyflow/react";
import { outcomeLabel, statusLabel } from "./graphLabels";
import type { FlowEdge } from "./graphLayout";

const GraphEdge = memo(function GraphEdge({ id, data, selected }: EdgeProps<FlowEdge>) {
  if (!data) return null;
  const object = data.object;
  const relation = "relation" in object;
  const text = relation ? object.relation : object.action;
  const status = relation ? object.status : object.outcome;
  const label = relation ? statusLabel(status) : outcomeLabel(status);
  const chars = Array.from(text);
  const preview = chars.length > 16 ? chars.slice(0, 16).join("") + "…" : text;
  return (
    <g className={'gedge ' + status + (selected ? ' is-selected' : '') + (data.related ? ' is-related' : '')}>
      <title>{text} · {label}</title>
      <BaseEdge id={id} path={data.path} className="gedge-line" interactionWidth={14} />
      <polygon className="garrow" points={data.arrow} />
      <text className="gedge-label" x={data.labelAt.x} y={data.labelAt.y} textAnchor="middle">{preview} · {label}</text>
    </g>
  );
});

export const GRAPH_EDGE_TYPES = { sessionEdge: GraphEdge };
