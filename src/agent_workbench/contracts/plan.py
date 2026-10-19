"""当前计划的只读投影；权威数据仍保存在 LangGraph checkpoint。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class PlanRead(BaseModel):
    revision: int
    steps: list[PlanStepRead]
    current_focus: CurrentFocusRead | None = None
    work_assessment: WorkAssessmentRead | None = None
    source: Literal["langgraph_checkpoint"] = "langgraph_checkpoint"
    #: 合同是当前权威；steps 仅作为旧计划只读投影。
    plan_source: Literal["plan_contracts", "legacy_plan"] = "legacy_plan"
    active_contract_ids: list[str] = Field(default_factory=list)
    contracts: list[TaskContractRead] = Field(default_factory=list)
    frontier: list[FrontierRead] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# P1 TaskContract：不可变版本 + checkpoint 精确选择
# --------------------------------------------------------------------------- #

ContractState = Literal[
    "proposed",
    "ready",
    "running",
    "done",
    "blocked",
    "needs_review",
    "stale",
    "legacy_done",
    "dropped",
]


class ContractCondition(BaseModel):
    """合同的准入条件；未知不被当成成立。"""

    condition_key: str | None = None
    description: str
    satisfied: bool = False
    external: bool = False
    producer_contract_id: str | None = None


class ProducedCondition(BaseModel):
    """合同预期产出的事实类型；不是已成立事实。"""

    condition_key: str | None = None
    kind: str
    subject: str | None = None
    identity_ref: str | None = None


class ExpectedOutcome(BaseModel):
    outcome: Literal["supported", "refuted", "condition_unmet", "observation_failed"]
    description: str


class AcceptanceSpec(BaseModel):
    evaluator_id: str
    evaluator_version: str
    evidence_types: list[str] = Field(default_factory=list)
    coverage_scope: dict[str, Any] = Field(default_factory=dict)
    negative_control: bool = False
    min_independent_operations: int = 1


class TaskContractCreate(BaseModel):
    """追加一个合同版本所需的输入。"""

    contract_id: str
    question: str = Field(min_length=1)
    kind: Literal["coverage", "followup", "mixed"] = "mixed"
    parent_contract_id: str | None = None
    parent_contract_revision: int | None = None
    goal_ref: str | None = None
    lead_ids: list[str] = Field(default_factory=list)
    requires: list[ContractCondition] = Field(default_factory=list)
    expected_outcomes: list[ExpectedOutcome] = Field(default_factory=list)
    produces: list[ProducedCondition] = Field(default_factory=list)
    acceptance: AcceptanceSpec
    state: ContractState = "proposed"
    state_reason: str | None = None
    resume_when: dict[str, Any] | None = None
    basis_versions: dict[str, int] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    acceptance_result_ref: str | None = None
    mutation_id: str
    based_on_checkpoint_ref: str | None = None


class TaskContractRead(TaskContractCreate):
    session_id: str
    contract_revision: int
    schema_version: int
    content_hash: str


class FrontierRead(BaseModel):
    contract_id: str
    contract_revision: int
    state: str
    eligible: bool
    rank: int
    reasons: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
