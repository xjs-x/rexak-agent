"""当前计划的只读投影；权威数据仍保存在 LangGraph checkpoint。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_workbench.contracts.planning import (
    PlanningSource,
    StepCompletionRecord,
    StepCompletionSpec,
)


class PlanStepRead(BaseModel):
    step_id: str
    description: str
    status: Literal["pending", "in_progress", "done", "dropped", "blocked"]
    #: 步骤类型：覆盖检查 / 链条跟进 / 混合。旧 checkpoint 缺失时按 `mixed` 读取。
    kind: Literal["coverage", "followup", "mixed"] = "mixed"
    rationale: str | None = None
    #: 兼容字段：本步骤的**主**线索。新写入与 `lead_ids` 冲突时以 `lead_ids` 为准。
    lead_id: str | None = None
    #: 本步骤服务的全部线索（一个步骤可服务多条线索）。
    lead_ids: list[str] = Field(default_factory=list)
    done_when: str | None = None
    resume_note: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    completion_protocol: Literal[1] | None = None
    completion_spec: StepCompletionSpec | None = None
    completion: StepCompletionRecord | None = None
    legacy_completion: bool = False


class CurrentFocusRead(BaseModel):
    step_id: str | None = None
    lead_id: str | None = None
    intent: str
    reason: str
    return_to_step_id: str | None = None
    #: 焦点来源：模型明确选择 / 由唯一进行中步骤承接。旧 checkpoint 可能缺失。
    source: Literal["explicit", "plan_inferred"] | None = None


class WorkAssessmentRead(BaseModel):
    """最近一次明确判断的只读投影（解释，不是漏洞事实）。"""

    step_id: str | None = None
    lead_id: str | None = None
    changed: str
    next_action: str
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainty: str | None = None
    source_tool_call_id: str
    revision: int
    evaluated_step_ids: list[str] = Field(default_factory=list)
    evaluated_plan_revision: int | None = None
    decision: str | None = None
    trigger: str | None = None
    basis_refs: list[PlanningSource] = Field(default_factory=list)
    source_signature: str | None = None
    next_focus: CurrentFocusRead | None = None
    resulting_plan_revision: int | None = None


class PlanRead(BaseModel):
    revision: int
    steps: list[PlanStepRead]
    current_focus: CurrentFocusRead | None = None
    work_assessment: WorkAssessmentRead | None = None
    last_plan_change: dict[str, object] | None = None
    revision_projection_error: str | None = None
    source: Literal["langgraph_checkpoint"] = "langgraph_checkpoint"
