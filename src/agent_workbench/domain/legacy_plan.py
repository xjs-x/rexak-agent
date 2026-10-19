"""旧 PlanStep -> TaskContract 的确定性迁移映射。

只做结构映射，不把旧 done 当成可核验完成；旧 done 一律 legacy_done。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5

from agent_workbench.contracts.plan import AcceptanceSpec, TaskContractCreate

_STATUS_MAP = {
    "pending": "proposed",
    "in_progress": "running",
    "done": "legacy_done",
    "blocked": "blocked",
    "dropped": "dropped",
}


def legacy_contract_id(session_id: UUID, step_id: str) -> UUID:
    return uuid5(session_id, f"legacy-plan-step:{step_id}")


def legacy_mutation_id(session_id: UUID, step_id: str) -> UUID:
    return uuid5(session_id, f"legacy-plan-mutation:{step_id}:v1")


def legacy_step_to_contract(
    session_id: UUID,
    step: Mapping[str, Any],
) -> TaskContractCreate:
    step_id = str(step.get("step_id") or "").strip()
    if not step_id:
        raise ValueError("旧计划步骤缺少 step_id")
    description = str(step.get("description") or "").strip()
    if not description:
        raise ValueError(f"旧计划步骤 {step_id} 缺少 description")
    status = str(step.get("status") or "pending")
    state = _STATUS_MAP.get(status)
    if state is None:
        raise ValueError(f"旧计划步骤 {step_id} 状态非法：{status}")
    kind = str(step.get("kind") or "mixed")
    if kind not in {"coverage", "followup", "mixed"}:
        kind = "mixed"
    lead_ids = [str(item) for item in (step.get("lead_ids") or []) if str(item).strip()]
    primary = str(step.get("lead_id") or "").strip()
    if primary and primary not in lead_ids:
        lead_ids.insert(0, primary)
    reason = None
    if state == "legacy_done":
        reason = "旧计划声称完成，但缺少可核验验收；需重新评估"
    elif state == "blocked":
        reason = str(step.get("rationale") or "").strip() or "旧计划标记受阻，原因缺失"
    return TaskContractCreate(
        contract_id=str(legacy_contract_id(session_id, step_id)),
        question=description,
        kind=kind,  # type: ignore[arg-type]
        lead_ids=lead_ids,
        acceptance=AcceptanceSpec(
            evaluator_id="legacy_plan_projection",
            evaluator_version="v1",
            evidence_types=[],
            coverage_scope={},
            negative_control=False,
            min_independent_operations=1,
        ),
        state=state,  # type: ignore[arg-type]
        state_reason=reason,
        basis_versions={},
        evidence_refs=[str(item) for item in (step.get("evidence_refs") or [])],
        mutation_id=str(legacy_mutation_id(session_id, step_id)),
        based_on_checkpoint_ref=None,
    )


def migrate_legacy_plan(
    session_id: UUID, plan: Sequence[Mapping[str, Any]] | None
) -> list[TaskContractCreate]:
    return [legacy_step_to_contract(session_id, step) for step in (plan or [])]


__all__ = [
    "legacy_contract_id",
    "legacy_mutation_id",
    "legacy_step_to_contract",
    "migrate_legacy_plan",
]
