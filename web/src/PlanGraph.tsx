import { useMemo } from "react";
import { ReactFlow, Background, Controls, Position, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { LeadItem, PlanRead } from "./api/client";

interface Props {
  plan: PlanRead;
  leads: LeadItem[];
}

export function PlanGraph({ plan, leads }: Props) {
  const { nodes, edges } = useMemo(() => {
    const nodes: Node[] = plan.steps.map((step, index) => ({
      id: `step:${step.step_id}`,
      position: { x: 20, y: index * 104 },
      sourcePosition: Position.Right,
      data: { label: `${step.step_id} · ${step.description.slice(0, 55)} (${step.status})` },
      style: { width: 300, background: "#20314a", color: "#e7f0fb", border: "1px solid #56729a" },
    }));
    const edges: Edge[] = [];
    const leadById = new Map(leads.map((lead) => [lead.id, lead]));
    const leadPositions = new Map<string, number>();
    plan.steps.forEach((step, index) => {
      if (!step.lead_id) return;
      if (!leadPositions.has(step.lead_id)) leadPositions.set(step.lead_id, index);
      edges.push({ id: `step-lead:${step.step_id}:${step.lead_id}`, source: `step:${step.step_id}`, target: `lead:${step.lead_id}` });
    });
    for (const [leadId, index] of leadPositions) {
      const lead = leadById.get(leadId);
      nodes.push({
        id: `lead:${leadId}`,
        position: { x: 420, y: index * 104 },
        targetPosition: Position.Left,
        data: { label: lead ? `线索 · ${lead.hypothesis.slice(0, 48)} (${lead.status})` : `关联线索 ${leadId}` },
        style: { width: 270, background: "#1a3a37", color: "#d8f6eb", border: "1px solid #3f8278" },
      });
    }
    return { nodes, edges };
  }, [plan, leads]);

  if (!nodes.length) return <p className="empty">尚无计划步骤</p>;
  return <div className="plan-graph" aria-label="计划步骤与关联线索的只读关系图">
    <ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}>
      <Background color="#34465c" gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
