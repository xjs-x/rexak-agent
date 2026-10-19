import { useMemo } from "react";
import { ReactFlow, Background, Controls, Position, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { LeadItem, PlanRead } from "./api/client";

interface ContractView {
  contract_id: string;
  contract_revision: number;
  question: string;
  state: string;
  lead_ids: string[];
  state_reason?: string | null;
}

interface FrontierView {
  contract_id: string;
  contract_revision: number;
  state: string;
  eligible: boolean;
  reasons: string[];
  missing_conditions: string[];
}

type PlanView = PlanRead & {
  plan_source?: "plan_contracts" | "legacy_plan";
  active_contract_ids?: string[];
  contracts?: ContractView[];
  frontier?: FrontierView[];
};

interface Props {
  plan: PlanView;
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
      const ids = [...new Set([...(step.lead_ids ?? []), ...(step.lead_id ? [step.lead_id] : [])])];
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

  const assessmentApplies = (plan.work_assessment?.step_id ?? null) === (plan.current_focus?.step_id ?? null)
    && (plan.work_assessment?.lead_id ?? null) === (plan.current_focus?.lead_id ?? null);
  return <>
    {plan.plan_source === "plan_contracts" && <div className="tool-detail" aria-label="合同工作集">
      <strong>合同工作集（{plan.contracts?.length ?? 0}）</strong>
      {(plan.frontier ?? []).map(item => <p key={`frontier:${item.contract_id}`}>
        frontier · {item.contract_id.slice(0, 8)} · {item.state} · {item.eligible ? "可推进" : "不可推进"}
        {item.missing_conditions.length ? `（缺：${item.missing_conditions.join("; ")}）` : ""}
      </p>)}
      {(plan.contracts ?? []).map(contract => <details key={`contract:${contract.contract_id}`}>
        <summary>{contract.contract_id.slice(0, 8)}@{contract.contract_revision} · {contract.question} · {contract.state}</summary>
        {contract.state_reason && <p>状态说明：{contract.state_reason}</p>}
        {contract.lead_ids.length > 0 && <p>关联线索：{contract.lead_ids.join(", ")}</p>}
      </details>)}
    </div>}
    {plan.current_focus && <div className="tool-detail" aria-label="当前行动焦点">
      <strong>当前行动：{plan.current_focus.intent}</strong>
      <p>理由：{plan.current_focus.reason}</p>
      <p>焦点来源：{plan.current_focus.source === "plan_inferred" ? "由唯一进行中步骤承接（只说明执行上下文，不证明证据支持该假设）" : "模型明确选择"}</p>
      {plan.current_focus.return_to_step_id && <p>回到步骤：{plan.current_focus.return_to_step_id}</p>}
    </div>}
    {plan.work_assessment ? <div className="tool-detail" aria-label="最近一次明确判断">
      <strong>{assessmentApplies ? "最近判断" : "此前焦点的判断"}：{plan.work_assessment.changed}</strong>
      <p>{assessmentApplies ? "下一步" : "此前拟定的下一步"}：{plan.work_assessment.next_action}</p>
      {plan.work_assessment.uncertainty && <p>不确定性：{plan.work_assessment.uncertainty}</p>}
      <p>修订：v{plan.work_assessment.revision}（解释，不是漏洞事实）</p>
    </div> : <p className="empty">尚未记录判断</p>}
    {plan.plan_source === "plan_contracts" && <p className="empty">以下为旧计划只读投影；当前权威是上方合同工作集。</p>}
    {nodes.length ? <div className="plan-graph" aria-label="计划步骤与关联线索的只读关系图">
    <ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}>
      <Background color="#34465c" gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
    </div> : <p className="empty">尚无计划步骤</p>}
    {plan.steps.map(step => <details key={step.step_id} className="tool-section">
      <summary>{step.step_id} · {step.description} · {step.status}</summary>
      {step.rationale && <p>行动理由：{step.rationale}</p>}
      {step.done_when && <p>完成条件：{step.done_when}</p>}
      {step.resume_note && <p>恢复提示：{step.resume_note}</p>}
      {(step.evidence_refs ?? []).map(ref => <p key={ref}>
        <a href={`/api/artifacts/${encodeURIComponent(ref)}?limit=65536`} target="_blank" rel="noreferrer">查看来源 {ref}</a>
      </p>)}
    </details>)}
  </>;
}
