"""旧 checkpoint plan -> TaskContract 的一次性迁移。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from sqlalchemy import or_, select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import Settings
from agent_workbench.domain.legacy_plan import migrate_legacy_plan
from agent_workbench.domain.plan_contracts import ContractMutationConflict, append_contract
from agent_workbench.domain.run_state import claim_run
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import (
    CheckpointHeadRow,
    DomainIntegrationRow,
    ExecutionWaitSetRow,
    RunRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)
from agent_workbench.runtime.checkpoint_fencing import FencedCheckpointSaver, StaleWriterError


async def _latest_run(db: Any, session_id: Any) -> RunRow | None:
    return (
        await db.execute(
            select(RunRow)
            .where(RunRow.session_id == session_id)
            .order_by(RunRow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _session_blockers(
    db: Any, session_id: Any, *, ignore_owner: str | None = None
) -> dict[str, list[str]]:
    """迁移前对整个 session 做只读对账；有未决副作用就不 apply。"""
    from agent_workbench.runtime.jobs import UNRESOLVED_JOB_STATES

    blockers: dict[str, list[str]] = {}
    now = datetime.now(UTC)
    leases = (
        await db.execute(
            select(RunRow.id, RunRow.owner).where(
                RunRow.session_id == session_id,
                RunRow.lease_until.is_not(None),
                RunRow.lease_until > now,
            )
        )
    ).all()
    leases = [
        (run_id, owner)
        for run_id, owner in leases
        if ignore_owner is None or owner != ignore_owner
    ]
    if leases:
        blockers["active_run_leases"] = [
            f"{run_id}:{owner or 'ownerless'}" for run_id, owner in leases
        ]
    jobs = (
        await db.execute(
            select(ToolJobRow.job_id, ToolJobRow.state, ToolJobRow.sync_status)
            .join(ToolOperationRow, ToolOperationRow.operation_id == ToolJobRow.operation_id)
            .where(
                ToolOperationRow.session_id == session_id,
                or_(
                    ToolJobRow.state.in_(list(UNRESOLVED_JOB_STATES)),
                    ToolJobRow.sync_status == "uncertain",
                ),
            )
        )
    ).all()
    if jobs:
        blockers["pending_jobs"] = list(
            dict.fromkeys(
                f"{job_id}:{state}/{sync_status}" for job_id, state, sync_status in jobs
            )
        )
    open_ops = (
        await db.execute(
            select(ToolOperationRow.operation_id, ToolOperationRow.status).where(
                ToolOperationRow.session_id == session_id,
                ToolOperationRow.status.in_(["accepted", "running", "unknown"]),
            )
        )
    ).all()
    if open_ops:
        blockers["open_operations"] = [
            f"{operation_id}:{status}" for operation_id, status in open_ops
        ]
    waits = (
        await db.execute(
            select(ExecutionWaitSetRow.id).where(
                ExecutionWaitSetRow.session_id == session_id,
                ExecutionWaitSetRow.status == "open",
            )
        )
    ).scalars().all()
    if waits:
        blockers["open_wait_sets"] = [str(item) for item in waits]
    integrations = (
        await db.execute(
            select(DomainIntegrationRow.integration_id).where(
                DomainIntegrationRow.session_id == session_id,
                DomainIntegrationRow.status.in_(["pending", "retryable", "conflict"]),
            )
        )
    ).scalars().all()
    if integrations:
        blockers["pending_integrations"] = [str(item) for item in integrations]
    return blockers


def _plan_digest(plan: list[Any], revision: int) -> str:
    payload = json.dumps(
        {"plan": plan, "revision": int(revision)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checkpoint_blockers(values: dict[str, Any]) -> dict[str, list[str]]:
    messages = list(values.get("messages") or [])
    result_ids: set[str] = set()
    calls: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            result_ids.add(str(message.tool_call_id))
            continue
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                call_id = str(call.get("id") or "")
                if call_id:
                    calls[call_id] = str(call.get("name") or "")
    unpaired = [f"{call_id}:{name}" for call_id, name in calls.items() if call_id not in result_ids]
    return {"unpaired_tool_calls": unpaired} if unpaired else {}


def _admission(
    run: RunRow,
    blockers: dict[str, list[str]],
    now: datetime,
    *,
    ignore_owner: str | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if (
        run.lease_until is not None
        and run.lease_until > now
        and run.owner != ignore_owner
    ):
        reasons.append("active_lease")
    if str(run.status) not in {"queued", "paused"}:
        reasons.append("run_status")
    if blockers:
        reasons.append("blockers")
    return {"apply_eligible": not reasons, "reasons": reasons}


async def _read_accepted(
    settings: Settings, thread_id: str, head: CheckpointHeadRow | None
) -> Any:
    base_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if head is not None and head.accepted_checkpoint_id:
        base_config["configurable"]["checkpoint_id"] = head.accepted_checkpoint_id
    async with open_checkpointer_connection(settings) as connection:
        return await AsyncPostgresSaver(connection).aget_tuple(cast(Any, base_config))


async def _claim_migration(
    database: Database, run_id: Any, owner: str
) -> tuple[bool, int, str | None]:
    """在同一 run 行锁内复核 queued/paused 与租约，并推进 generation 撤销旧 writer。"""
    async with database.transaction() as db:
        row = await db.get(RunRow, run_id, with_for_update=True)
        if row is None:
            return False, 0, "no_run"
        now = datetime.now(UTC)
        if row.lease_until is not None and row.lease_until > now:
            return False, int(row.owner_generation), "active_lease"
        if str(row.status) not in {"queued", "paused"}:
            return False, int(row.owner_generation), "run_status"
        acquired, generation = await claim_run(
            db, run_id=run_id, owner=owner, lease_seconds=300
        )
        if not acquired:
            return False, int(row.owner_generation), "claim_denied"
        locked = await db.get(RunRow, run_id, with_for_update=True, populate_existing=True)
        if locked is None:
            return False, 0, "no_run"
        if str(locked.status) not in {"queued", "paused"}:
            locked.owner = None
            locked.lease_until = None
            await db.flush()
            return False, int(locked.owner_generation), "run_status"
        # claim_run 已在新 owner/过期接管时推进 generation。
        locked.owner = owner
        locked.lease_until = now + timedelta(seconds=300)
        await db.flush()
        return True, int(generation), None


async def migrate_legacy_checkpoint(
    database: Database,
    settings: Settings,
    session_id: Any,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """读取旧 plan，生成 legacy contracts；apply 时经受控保存入口写 checkpoint。"""
    async with database.session() as db:
        session = await db.get(SessionRow, session_id)
        if session is None:
            return {"status": "not_found", "session_id": str(session_id)}
        thread_id = session.thread_id
        run = await _latest_run(db, session_id)
        head = await db.get(CheckpointHeadRow, (thread_id, ""))
        blockers = await _session_blockers(db, session_id)
    if run is None:
        return {"status": "no_run", "session_id": str(session_id)}

    saved = await _read_accepted(settings, thread_id, head)
    values = saved.checkpoint.get("channel_values", {}) if saved is not None else {}
    if values.get("contract_refs"):
        return {
            "status": "already_migrated",
            "session_id": str(session_id),
            "contract_refs": list(values["contract_refs"]),
        }
    plan = list(values.get("plan") or [])
    if not plan:
        return {"status": "no_legacy_plan", "session_id": str(session_id)}
    blockers = {**blockers, **_checkpoint_blockers(values)}

    contracts = migrate_legacy_plan(session_id, plan)
    base_checkpoint_id = (
        (saved.config.get("configurable") or {}).get("checkpoint_id") if saved else None
    )
    base_digest = _plan_digest(plan, int(values.get("plan_revision") or 0))
    admission = _admission(run, blockers, datetime.now(UTC))
    if not apply:
        return {
            "status": "dry_run",
            "session_id": str(session_id),
            "base_checkpoint_id": base_checkpoint_id,
            **admission,
            "blockers": blockers,
            "contracts": [
                {
                    "contract_id": item.contract_id,
                    "state": item.state,
                    "question": item.question,
                }
                for item in contracts
            ],
        }

    if "active_run_leases" in blockers:
        return {
            "status": "refused_active_lease",
            "session_id": str(session_id),
            "blockers": blockers,
        }
    if blockers:
        return {
            "status": "needs_reconcile",
            "session_id": str(session_id),
            "blockers": blockers,
        }
    migration_owner = f"plan-migration:{uuid4()}"
    claimed, generation, reason = await _claim_migration(
        database, run.id, migration_owner
    )
    if not claimed:
        return {
            "status": (
                "refused_active_lease" if reason == "active_lease" else "refused_active_run"
            ),
            "session_id": str(session_id),
            "run_status": str(run.status),
            "reason": reason,
        }

    try:
        # claim 后复检：状态/租约/blockers 任一变化都要停下。
        async with database.session() as db:
            current_run = await db.get(RunRow, run.id, populate_existing=True)
            current_blockers = await _session_blockers(
                db, session_id, ignore_owner=migration_owner
            )
            current_head = await db.get(CheckpointHeadRow, (thread_id, ""))
        if current_run is None or not _admission(
            current_run,
            current_blockers,
            datetime.now(UTC),
            ignore_owner=migration_owner,
        )["apply_eligible"]:
            return {
                "status": "needs_reconcile",
                "session_id": str(session_id),
                "blockers": current_blockers,
                "reason": "state_changed_after_claim",
            }
        current_saved = await _read_accepted(settings, thread_id, current_head)
        current_values = (
            current_saved.checkpoint.get("channel_values", {}) if current_saved else {}
        )
        current_blockers = {
            **current_blockers,
            **_checkpoint_blockers(current_values),
        }
        current_plan = list(current_values.get("plan") or [])
        current_digest = _plan_digest(
            current_plan, int(current_values.get("plan_revision") or 0)
        )
        current_checkpoint_id = (
            (current_saved.config.get("configurable") or {}).get("checkpoint_id")
            if current_saved
            else None
        )
        if (
            current_checkpoint_id != base_checkpoint_id
            or current_digest != base_digest
        ):
            return {
                "status": "stale_baseline",
                "session_id": str(session_id),
                "expected_checkpoint_id": base_checkpoint_id,
                "actual_checkpoint_id": current_checkpoint_id,
            }

        refs: list[dict[str, Any]] = []
        try:
            async with database.transaction() as db:
                for contract in contracts:
                    row = await append_contract(
                        db, session_id=session_id, run_id=run.id, contract=contract
                    )
                    refs.append(
                        {
                            "contract_id": str(row.contract_id),
                            "contract_revision": int(row.contract_revision),
                            "content_hash": row.content_hash,
                        }
                    )
        except ContractMutationConflict as exc:
            return {
                "status": "migration_conflict",
                "session_id": str(session_id),
                "reason": str(exc),
            }

        async with open_checkpointer_connection(settings) as connection:
            raw = AsyncPostgresSaver(connection)
            fenced = FencedCheckpointSaver(
                raw,
                database,
                run_id=run.id,
                thread_id=thread_id,
                owner_generation=generation,
                control_version=int(run.control_version),
                writer_epoch=int(current_head.writer_epoch)
                if current_head is not None
                else 0,
                expected_checkpoint_id=base_checkpoint_id,
                writer_owner=migration_owner,
            )
            graph = StateGraph(AgentGraphState)
            graph.add_node(
                "noop",
                lambda state: {
                    "plan_revision": int(cast(Any, state).get("plan_revision") or 0)
                },
            )
            graph.set_entry_point("noop")
            graph.set_finish_point("noop")
            compiled = graph.compile(checkpointer=fenced)
            try:
                await compiled.aupdate_state(
                    {"configurable": {"thread_id": thread_id}},
                    {
                        "messages": [],
                        "contract_refs": refs,
                        "active_contract_ids": [item["contract_id"] for item in refs],
                        "plan_revision": int(current_values.get("plan_revision") or 0) + 1,
                    },
                )
            except StaleWriterError:
                return {
                    "status": "stale_baseline",
                    "session_id": str(session_id),
                    "expected_checkpoint_id": base_checkpoint_id,
                }
    finally:
        async with database.transaction() as db:
            locked = await db.get(RunRow, run.id, with_for_update=True)
            if locked is not None and locked.owner == migration_owner:
                locked.owner = None
                locked.lease_until = None
                await db.flush()
    return {
        "status": "migrated",
        "session_id": str(session_id),
        "contract_refs": refs,
    }


__all__ = ["migrate_legacy_checkpoint"]
