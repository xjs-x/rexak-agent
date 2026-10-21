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

  const assessedFocus = plan.work_assessment?.evaluated_plan_revision != null
    ? plan.work_assessment.next_focus : plan.work_assessment;
  const assessmentApplies = (assessedFocus?.step_id ?? null) === (plan.current_focus?.step_id ?? null)
    && (assessedFocus?.lead_id ?? null) === (plan.current_focus?.lead_id ?? null)
    && (plan.work_assessment?.evaluated_plan_revision == null
      || (plan.work_assessment.resulting_plan_revision === plan.revision
        && plan.work_assessment.next_focus?.intent === plan.current_focus?.intent
        && plan.work_assessment.next_focus?.reason === plan.current_focus?.reason
        && plan.work_assessment.next_focus?.return_to_step_id === plan.current_focus?.return_to_step_id
        && plan.work_assessment.next_focus?.source === plan.current_focus?.source));
  return <>
    {plan.revision_projection_error && <p role="status">{plan.revision_projection_error}；当前计划仍以已保存状态为准。</p>}
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
      {plan.work_assessment.evaluated_plan_revision != null && <p>
        评估对象：计划 v{plan.work_assessment.evaluated_plan_revision} · {plan.work_assessment.evaluated_step_ids?.join("、") || "初始探索"}
      </p>}
      <p>修订：v{plan.work_assessment.revision}（解释，不是漏洞事实）</p>
    </div> : <p className="empty">尚未记录判断</p>}
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
      {step.completion_spec && <p>完成方式：{step.completion_spec.mode === "investigate" ? "取得明确调查结果" : "达成预期条件"}</p>}
      {step.completion && <>
        <p>实际结果：{step.completion.outcome === "satisfied" ? "预期条件成立" : step.completion.outcome === "not_satisfied" ? "预期条件不成立" : "尚不能判断"} · {step.completion.summary}</p>
        <p>检查范围：{step.completion.check.scope}</p>
        {step.completion.check.sufficiency && <p>结论依据：{step.completion.check.sufficiency === "model_judgment" ? "模型判断，未经独立语义验证" : step.completion.check.sufficiency === "receipt_fields" ? "指定回执字段" : "所引用验证记录的范围"}</p>}
        {(step.completion.check.http_coverage ?? []).map(item => <p key={item.operation_id + item.method + item.url}>已检查：{item.method} {item.url} → {item.status_code}</p>)}
        {(step.completion.check.limitations ?? []).map(note => <p key={note}>限制：{note}</p>)}
      </>}
      {step.legacy_completion && <p>历史完成尚未按当前条件复查。</p>}
      {step.resume_note && <p>恢复提示：{step.resume_note}</p>}
      {(step.evidence_refs ?? []).map(ref => <p key={ref}>
        <a href={`/api/artifacts/${encodeURIComponent(ref)}?limit=65536`} target="_blank" rel="noreferrer">查看来源 {ref}</a>
      </p>)}
    </details>)}
  </>;
}
