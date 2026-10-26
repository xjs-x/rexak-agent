"""请求期只读投影；不定义持久事实或新的执行状态。"""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConditionDifference(MemoryValue):
    field: str
    status: Literal["comparable_same", "changed", "insufficient"]
    old_value: Any = None
    new_value: Any = None
    source_refs: list[str]
    limitation: str = "仅比较已记录维度，不证明测试等价或前提已满足。"


class MemoryUnit(MemoryValue):
    record_ref: str
    origin_kind: Literal["execution_observation", "model_claim", "plan_intent", "summary_claim"]
    statement: Any
    source_refs: list[str] = Field(default_factory=list)
    match_reasons: list[str] = Field(default_factory=list)
    support_status: str = "semantic_unverified"
    source_status: str = "not_independently_checked"
    execution: dict[str, Any] = Field(default_factory=dict)
    observed_dimensions: dict[str, Any] = Field(default_factory=dict)
    evidence_availability: str = "metadata_only"


class DecisionBlackboard(MemoryValue):
    schema_version: Literal["decision_blackboard/1"] = "decision_blackboard/1"
    selector_version: Literal["deterministic-association/1"] = "deterministic-association/1"
    snapshot: dict[str, Any]
    current_focus: dict[str, Any]
    relevant_evidence: list[MemoryUnit] = Field(default_factory=list)
    previous_attempts: list[MemoryUnit] = Field(default_factory=list)
    open_items: list[MemoryUnit] = Field(default_factory=list)
    unresolved_results: list[str] = Field(default_factory=list)
    condition_differences: dict[str, list[ConditionDifference]] = Field(default_factory=dict)
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_status: dict[str, Any]


@dataclass(frozen=True)
class DecisionInput:
    session_id: str
    run_id: str
    control_version: int
    state: dict[str, Any]
    user_inputs: list[dict[str, Any]] = field(default_factory=list)
    focus_freshness: str = "unproven"
    explicit_refs: tuple[str, ...] = ()
    executor_id: str | None = None


@dataclass(frozen=True)
class MemorySnapshot:
    """只读适配器已限定 Session、脱敏及可用性的普通值；纯选择器不访问 IO。"""

    boundary: dict[str, Any]
    operations: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    claims: tuple[dict[str, Any], ...] = ()
    leads: tuple[dict[str, Any], ...] = ()
    conditions: dict[str, Any] = field(default_factory=dict)
    condition_refs: dict[str, str] = field(default_factory=dict)
    identity_revisions: dict[str, int] = field(default_factory=dict)
