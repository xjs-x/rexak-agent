"""不可变 TaskContract 版本与状态迁移。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.plan import (
    AcceptanceSpec,
    ContractCondition,
    ExpectedOutcome,
    ProducedCondition,
    TaskContractCreate,
)
from agent_workbench.persistence.tables import PlanContractRow

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"ready", "blocked", "needs_review", "dropped"}),
    "ready": frozenset({"running", "blocked", "needs_review", "stale", "dropped"}),
    "running": frozenset({"done", "blocked", "needs_review", "stale", "dropped"}),
    "done": frozenset({"stale", "needs_review"}),
    "blocked": frozenset({"ready", "needs_review", "dropped"}),
    "needs_review": frozenset({"ready", "blocked", "done", "dropped"}),
    "stale": frozenset({"needs_review", "ready", "blocked", "dropped"}),
    "legacy_done": frozenset({"needs_review", "done", "dropped"}),
    "dropped": frozenset({"needs_review"}),
}


class ContractValidationError(ValueError):
    pass


class ContractMutationConflict(RuntimeError):
    pass


def contract_content_hash(contract: TaskContractCreate) -> str:
    payload = contract.model_dump(mode="json", exclude={"mutation_id", "based_on_checkpoint_ref"})
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


async def _require_parent(
    db: AsyncSession, session_id: UUID, contract: TaskContractCreate
) -> None:
    if contract.parent_contract_id is None:
        return
    if contract.parent_contract_revision is None:
        raise ContractValidationError("parent_contract_id 必须同时给出 parent_contract_revision")
    try:
        parent_id = UUID(contract.parent_contract_id)
    except ValueError as exc:
        raise ContractValidationError("parent_contract_id 不是合法 UUID") from exc
    parent = await db.get(
        PlanContractRow, (session_id, parent_id, int(contract.parent_contract_revision))
    )
    if parent is None:
        raise ContractValidationError("父合同版本不存在")


async def append_contract(
    db: AsyncSession,
    *,
    session_id: UUID,
    run_id: UUID | None,
    contract: TaskContractCreate,
) -> PlanContractRow:
    """追加一个合同版本；同一 mutation 幂等，内容不同则冲突。"""
    try:
        contract_id = UUID(contract.contract_id)
        mutation_id = UUID(contract.mutation_id)
    except ValueError as exc:
        raise ContractValidationError("contract_id / mutation_id 必须是合法 UUID") from exc
    if contract.state not in ALLOWED_TRANSITIONS:
        raise ContractValidationError(f"合同状态非法：{contract.state}")
    await _require_parent(db, session_id, contract)

    digest = contract_content_hash(contract)
    existing = (
        await db.execute(
            select(PlanContractRow).where(
                PlanContractRow.session_id == session_id,
                PlanContractRow.mutation_id == mutation_id,
                PlanContractRow.contract_id == contract_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash != digest:
            raise ContractMutationConflict("同一 mutation 不能改写为不同合同内容")
        return existing

    latest = (
        await db.execute(
            select(PlanContractRow)
            .where(
                PlanContractRow.session_id == session_id,
                PlanContractRow.contract_id == contract_id,
            )
            .order_by(PlanContractRow.contract_revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        latest is not None
        and contract.state != latest.state
        and contract.state not in ALLOWED_TRANSITIONS.get(latest.state, frozenset())
    ):
        raise ContractValidationError(
            f"非法合同状态迁移：{latest.state} -> {contract.state}"
        )
    revision = int(latest.contract_revision) + 1 if latest is not None else 1

    row = PlanContractRow(
        session_id=session_id,
        contract_id=contract_id,
        contract_revision=revision,
        schema_version=1,
        parent_contract_id=UUID(contract.parent_contract_id)
        if contract.parent_contract_id
        else None,
        parent_contract_revision=contract.parent_contract_revision,
        goal_ref=contract.goal_ref,
        question=contract.question,
        kind=contract.kind,
        lead_ids=list(contract.lead_ids),
        requires_json=[item.model_dump(mode="json") for item in contract.requires],
        expected_outcomes_json=[
            item.model_dump(mode="json") for item in contract.expected_outcomes
        ],
        produces_json=[item.model_dump(mode="json") for item in contract.produces],
        acceptance_json=contract.acceptance.model_dump(mode="json"),
        state=contract.state,
        state_reason=contract.state_reason,
        resume_when_json=contract.resume_when,
        basis_versions=dict(contract.basis_versions),
        evidence_refs=list(contract.evidence_refs),
        acceptance_result_ref=contract.acceptance_result_ref,
        mutation_id=mutation_id,
        based_on_checkpoint_ref=contract.based_on_checkpoint_ref,
        content_hash=digest,
        created_by_run_id=run_id,
    )
    db.add(row)
    await db.flush()
    return row


async def append_transition(
    db: AsyncSession,
    *,
    session_id: UUID,
    contract_id: UUID,
    to_state: str,
    mutation_id: UUID,
    run_id: UUID | None,
    state_reason: str | None = None,
    evidence_refs: list[str] | None = None,
    acceptance_result_ref: str | None = None,
    based_on_checkpoint_ref: str | None = None,
) -> PlanContractRow:
    latest = (
        await db.execute(
            select(PlanContractRow)
            .where(
                PlanContractRow.session_id == session_id,
                PlanContractRow.contract_id == contract_id,
            )
            .order_by(PlanContractRow.contract_revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise ContractValidationError("合同不存在")
    if to_state not in ALLOWED_TRANSITIONS.get(latest.state, frozenset()):
        raise ContractValidationError(f"非法合同状态迁移：{latest.state} -> {to_state}")
    contract = TaskContractCreate(
        contract_id=str(contract_id),
        question=latest.question,
        kind=latest.kind,  # type: ignore[arg-type]
        parent_contract_id=str(latest.parent_contract_id) if latest.parent_contract_id else None,
        parent_contract_revision=latest.parent_contract_revision,
        goal_ref=latest.goal_ref,
        lead_ids=list(latest.lead_ids or []),
        requires=[ContractCondition.model_validate(item) for item in (latest.requires_json or [])],
        expected_outcomes=[
            ExpectedOutcome.model_validate(item) for item in (latest.expected_outcomes_json or [])
        ],
        produces=[ProducedCondition.model_validate(item) for item in (latest.produces_json or [])],
        acceptance=AcceptanceSpec.model_validate(latest.acceptance_json or {}),
        state=to_state,  # type: ignore[arg-type]
        state_reason=state_reason,
        resume_when=latest.resume_when_json,
        basis_versions=dict(latest.basis_versions or {}),
        evidence_refs=list(evidence_refs if evidence_refs is not None else latest.evidence_refs),
        acceptance_result_ref=acceptance_result_ref or latest.acceptance_result_ref,
        mutation_id=str(mutation_id),
        based_on_checkpoint_ref=based_on_checkpoint_ref,
    )
    return await append_contract(db, session_id=session_id, run_id=run_id, contract=contract)


async def get_contract(
    db: AsyncSession, session_id: UUID, contract_id: UUID, revision: int
) -> PlanContractRow | None:
    return await db.get(PlanContractRow, (session_id, contract_id, revision))


async def latest_contract(
    db: AsyncSession, session_id: UUID, contract_id: UUID
) -> PlanContractRow | None:
    return (
        await db.execute(
            select(PlanContractRow)
            .where(
                PlanContractRow.session_id == session_id,
                PlanContractRow.contract_id == contract_id,
            )
            .order_by(PlanContractRow.contract_revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ContractMutationConflict",
    "ContractValidationError",
    "append_contract",
    "append_transition",
    "contract_content_hash",
    "get_contract",
    "latest_contract",
    "load_contract_refs",
]


async def load_contract_refs(
    db: AsyncSession, session_id: UUID, refs: Sequence[Mapping[str, Any]] | None
) -> list[PlanContractRow]:
    rows: list[PlanContractRow] = []
    for ref in refs or []:
        try:
            contract_id = UUID(str(ref["contract_id"]))
            revision = int(ref["contract_revision"])
            digest = str(ref["content_hash"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractValidationError("合同引用格式非法") from exc
        row = await get_contract(db, session_id, contract_id, revision)
        if row is None:
            raise ContractValidationError(f"合同版本不存在：{contract_id}@{revision}")
        if row.content_hash != digest:
            raise ContractValidationError(f"合同内容哈希不匹配：{contract_id}@{revision}")
        rows.append(row)
    return rows
