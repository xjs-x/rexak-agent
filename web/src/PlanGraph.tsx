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
      data: { label: `${plan.current_focus?.step_id === step.step_id ? "当前焦点 · " : ""}${step.step_id} · ${step.description.slice(0, 55)} (${step.status})` },
      style: { width: 300, background: "#20314a", color: "#e7f0fb", border: plan.current_focus?.step_id === step.step_id ? "2px solid #63ccb6" : "1px solid #56729a" },
    }));
    const edges: Edge[] = [];
    const leadById = new Map(leads.map((lead) => [lead.id, lead]));
    const leadPositions = new Map<string, number>();
    plan.steps.forEach(step => {
      const ids = [...new Set([...(step.lead_id ? [step.lead_id] : [])])];
      ids.forEach(leadId => {
        if (!leadPositions.has(leadId)) leadPositions.set(leadId, leadPositions.size);
        edges.push({ id: `step-lead:${step.step_id}:${leadId}`, source: `step:${step.step_id}`, target: `lead:${leadId}` });
      });
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

  return <>
    {plan.revision_projection_error && <p role="status">{plan.revision_projection_error}；当前计划仍以已保存状态为准。</p>}
    {plan.current_focus && <div className="tool-detail" aria-label="当前行动焦点">
      <strong>当前行动：{plan.current_focus.intent}</strong>
      {plan.current_focus.reason && <p>理由：{plan.current_focus.reason}</p>}
      <p>焦点来源：{plan.current_focus.source === "plan_inferred" ? "由唯一进行中步骤承接（只说明执行上下文，不证明证据支持该假设）" : "模型明确选择"}</p>
      {plan.current_focus.return_to_step_id && <p>回到步骤：{plan.current_focus.return_to_step_id}</p>}
    </div>}
    {nodes.length ? <div className="plan-graph" aria-label="计划步骤与关联线索的只读关系图">
    <ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}>
      <Background color="#34465c" gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
    </div> : <p className="empty">尚无计划步骤，可直接执行工具</p>}
    {plan.steps.map(step => <details key={step.step_id} className="tool-section">
      <summary>{step.step_id} · {step.description} · {step.status}</summary>
      {step.rationale && <p>行动理由：{step.rationale}</p>}
      {step.legacy_metadata && <details><summary>历史计划附加信息（已停用的协议）</summary><pre>{JSON.stringify(step.legacy_metadata, null, 2)}</pre></details>}
    </details>)}
  </>;
}
