"""Shadow DTO：只读执行来源与独立触发状态；没有执行或修改 Agent 的能力。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.contracts.decision_memory import ConditionDifference

Category = Literal[
    "TRIGGER_CANDIDATE",
    "RELATED_ONLY",
    "UNKNOWN_SEMANTICS",
    "NO_RELEVANT_HISTORY",
    "NOT_IN_SCOPE",
]
Policy = Literal["raw_visible_v1", "receipt_allowed_v1"]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Execution(Value):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    operation_ref: str | None = None
    semantic_contract: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    # 来源必须由可信适配器构造，不能接受模型自行声明的 verified。
    condition_sources: dict[str, list[str]] = Field(default_factory=dict)
    provenance_verified: bool = False
    origin: Literal["recorder", "model_claim", "summary_claim"] = "recorder"
    source_refs: list[str] = Field(default_factory=list)
    logical_test_key: str | None = None
    related_operation_refs: list[str] = Field(default_factory=list)


class HistoricalExecution(Execution):
    session_id: str
    status: str = "unknown"
    job_refs: list[str] = Field(default_factory=list)
    job_states: dict[str, str] = Field(default_factory=dict)
    finished_step: int | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class Evidence(Value):
    evidence_ref: str
    operation_ref: str
    session_id: str
    kind: Literal["artifact", "execution_receipt", "model_claim", "summary_claim"]
    content: str | None = None
    sha256: str | None = None
    visible: bool = False
    complete: bool = False
    provenance_verified: bool = False
    available_step: int
    source_tool_call_id: str | None = None
    source_refs: list[str] = Field(default_factory=list)


class TriggerState(Value):
    dedup_key: str
    status: Literal["triggered", "feedback_recorded", "agent_continued", "agent_changed"]
    source_ref: str


class Snapshot(Value):
    snapshot_id: str
    session_id: str
    task_version: str
    context_version: str
    evidence_version: str
    decision_step: int
    candidate: Execution
    history: list[HistoricalExecution] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    same_batch_tool_call_ids: list[str] = Field(default_factory=list)
    retest_intent: Literal["required", "not_required", "unspecified"] = "unspecified"
    previous_triggers: list[TriggerState] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class Match(Value):
    operation_ref: str
    retrieval_reasons: list[str]
    compared_conditions: list[ConditionDifference] = Field(default_factory=list)
    common_conditions: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    nontrigger_reasons: list[str] = Field(default_factory=list)
    unknown_execution: bool = False
    high_confidence_overlap: bool = False


class Observation(Value):
    schema_version: Literal["history_action_reflection/1"] = "history_action_reflection/1"
    snapshot_id: str
    candidate_tool_call_id: str
    candidate_tool_name: str
    predecision_summary: dict[str, Any]
    classification: Category
    policy: Policy
    related_history: list[Match] = Field(default_factory=list)
    trigger_reasons: list[str] = Field(default_factory=list)
    nontrigger_reasons: list[str] = Field(default_factory=list)
    original_evidence_refs: list[str] = Field(default_factory=list)
    dedup_key: str | None = None
    emit_shadow_event: bool = False
    duplicate_suppressed: bool = False
    neutral_message_draft: str | None = None
    diagnostics: list[str] = Field(default_factory=list)
    mode: Literal["SHADOW_ONLY"] = "SHADOW_ONLY"
