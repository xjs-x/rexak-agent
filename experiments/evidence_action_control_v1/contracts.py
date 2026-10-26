"""离线观察合同；没有执行命令、可写 Agent 状态或持久事实权限。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.contracts.decision_memory import ConditionDifference

EquivalenceLabel = Literal[
    "EXACT_REPEAT",
    "SEMANTIC_REPEAT",
    "CONDITION_CHANGED",
    "INSUFFICIENT_INFORMATION",
    "UNRELATED_ACTION",
]
SufficiencyLabel = Literal["SUFFICIENT", "PARTIAL", "INSUFFICIENT", "INDETERMINATE"]
ValueLabel = Literal["HIGH", "POTENTIAL", "LOW", "UNCERTAIN"]
EvidenceKind = Literal[
    "artifact_body",
    "execution_metadata",
    "execution_state",
    "model_claim",
    "summary_claim",
    "unavailable",
]


class ObservationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskQuestion(ObservationValue):
    goal: Literal[
        "interpret_history",
        "verify_current",
        "investigate_unknown",
        "progress_blocked",
        "unspecified",
    ] = "unspecified"
    text: str
    focus_conditions: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)


class CandidateAction(ObservationValue):
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    kind: Literal["test", "read", "query", "other"] = "other"
    conditions: dict[str, Any] = Field(default_factory=dict)
    information_key: str | None = None
    operation_ref: str | None = None
    artifact_ref: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    estimated_cost: dict[str, Any] = Field(default_factory=dict)


class HistoricalAction(ObservationValue):
    record_ref: str
    tool_name: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    information_key: str | None = None
    status: str = "unknown"
    job_refs: list[str] = Field(default_factory=list)
    job_states: dict[str, str] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    origin_kind: Literal["execution_observation", "model_claim", "summary_claim"] = (
        "execution_observation"
    )


class AvailableEvidence(ObservationValue):
    record_ref: str
    kind: EvidenceKind
    operation_ref: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    available: bool = True
    visible: bool = False
    complete: bool = False
    provenance_verified: bool = False
    content: str | None = None
    sha256: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    visibility_refs: list[str] = Field(default_factory=list)


class ActionSnapshot(ObservationValue):
    snapshot_id: str
    task: TaskQuestion
    candidate: CandidateAction
    historical_actions: list[HistoricalAction] = Field(default_factory=list)
    evidence: list[AvailableEvidence] = Field(default_factory=list)
    boundary: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class EquivalenceAssessment(ObservationValue):
    classification: EquivalenceLabel
    logical_classification: EquivalenceLabel
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    compared_dimensions: list[ConditionDifference] = Field(default_factory=list)
    material_differences: list[str] = Field(default_factory=list)
    unknown_dimensions: list[str] = Field(default_factory=list)
    classification_reason: str


class SufficiencyAssessment(ObservationValue):
    status: SufficiencyLabel
    supported_conclusions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    supporting_evidence_refs: list[str] = Field(default_factory=list)
    classification_reason: str


class GainAssessment(ObservationValue):
    classification: ValueLabel
    expected_new_information: list[str] = Field(default_factory=list)
    unresolved_question: str
    supporting_evidence_refs: list[str] = Field(default_factory=list)
    condition_differences: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    estimated_action_value: ValueLabel
    cost_and_side_effects: dict[str, Any] = Field(default_factory=dict)
    classification_reason: str


class RepeatRisk(ObservationValue):
    status: Literal["HIGH", "POSSIBLE", "LOW", "UNCERTAIN"]
    unknown_execution_risk: bool = False
    classification_reason: str


class ActionObservation(ObservationValue):
    schema_version: Literal["evidence_action_observation/1"] = "evidence_action_observation/1"
    snapshot_id: str
    candidate_action: CandidateAction
    related_historical_actions: list[str]
    equivalence: EquivalenceAssessment
    evidence_sufficiency: SufficiencyAssessment
    information_gain: GainAssessment
    repeat_risk: RepeatRisk
    source_refs: list[str]
    uncertainties: list[str] = Field(default_factory=list)
    recommended_feedback: str
    decision_mode: Literal["OBSERVE_ONLY"] = "OBSERVE_ONLY"
