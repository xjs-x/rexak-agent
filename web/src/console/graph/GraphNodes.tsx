import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { FlowNode } from "./graphLayout";

const GraphNode = memo(function GraphNode({ data, selected }: NodeProps<FlowNode>) {
  const entity = "label" in data.object;
  return (
    <div className={'gnode' + (selected ? ' is-selected' : '') + (data.related ? ' is-related' : '')} title={data.name}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Handle type="source" position={Position.Right} isConnectable={false} />
      <span className="gnode-icon" aria-hidden="true">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.45">
          {entity ? <path d="m10 3 6 3.5v7L10 17l-6-3.5v-7L10 3Zm-6 3.5 6 3.5 6-3.5M10 10v7" /> :
            <><circle cx="10" cy="10" r="6" /><circle cx="10" cy="10" r="2" /></>}
        </svg>
      </span>
      <span className="gnode-copy">
        <span className="gnode-title">{data.name}</span>
        <span className="gnode-meta">{entity ? "实体" : "状态"} · {data.degree} 条{entity ? "关系" : "尝试"}</span>
      </span>
    </div>
  );
});

export const GRAPH_NODE_TYPES = { sessionNode: GraphNode };
