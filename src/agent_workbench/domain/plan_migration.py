"""旧 checkpoint plan -> TaskContract 的一次性迁移。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from sqlalchemy import or_, select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import Settings
from agent_workbench.domain.legacy_plan import migrate_legacy_plan
from agent_workbench.domain.plan_contracts import append_contract
from agent_workbench.domain.run_state import claim_run, release_run
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
from agent_workbench.runtime.checkpoint_fencing import FencedCheckpointSaver


async def _latest_run(db: Any, session_id: Any) -> RunRow | None:
    return (
        await db.execute(
            select(RunRow)
            .where(RunRow.session_id == session_id)
            .order_by(RunRow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _session_blockers(db: Any, session_id: Any) -> dict[str, list[str]]:
    """迁移前对整个 session 做只读对账；有未决副作用就不 apply。"""
    from agent_workbench.runtime.jobs import UNRESOLVED_JOB_STATES

    blockers: dict[str, list[str]] = {}
    jobs = (
        await db.execute(
            select(ToolJobRow.job_id, ToolJobRow.state, ToolJobRow.sync_status)
            .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
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
        blockers["pending_jobs"] = [
            f"{job_id}:{state}/{sync_status}" for job_id, state, sync_status in jobs
        ]
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


async def _read_accepted(
    settings: Settings, thread_id: str, head: CheckpointHeadRow | None
) -> Any:
    base_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if head is not None and head.accepted_checkpoint_id:
        base_config["configurable"]["checkpoint_id"] = head.accepted_checkpoint_id
    async with open_checkpointer_connection(settings) as connection:
        return await AsyncPostgresSaver(connection).aget_tuple(cast(Any, base_config))


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

    contracts = migrate_legacy_plan(session_id, plan)
    if not apply:
        return {
            "status": "dry_run",
            "session_id": str(session_id),
            "base_checkpoint_id": (saved.config.get("configurable") or {}).get(
                "checkpoint_id"
            )
            if saved
            else None,
            "apply_eligible": not blockers,
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

    now = datetime.now(UTC)
    if run.lease_until is not None and run.lease_until > now:
        return {
            "status": "refused_active_lease",
            "session_id": str(session_id),
            "run_status": str(run.status),
        }
    if str(run.status) not in {"queued", "paused"}:
        return {
            "status": "refused_active_run",
            "session_id": str(session_id),
            "run_status": str(run.status),
            "note": "running/等待中的 run 必须先恢复对账并安全暂停；不会直接迁移。",
        }
    if blockers:
        return {
            "status": "needs_reconcile",
            "session_id": str(session_id),
            "blockers": blockers,
        }

    migration_owner = f"plan-migration:{uuid4()}"
    async with database.transaction() as db:
        acquired, generation = await claim_run(
            db, run_id=run.id, owner=migration_owner, lease_seconds=300
        )
    if not acquired:
        return {
            "status": "refused_active_run",
            "session_id": str(session_id),
            "run_status": str(run.status),
        }

    try:
        # 取得排他权后重新检查，避免“检查时无租约、迁移时已被认领”。
        async with database.session() as db:
            blockers = await _session_blockers(db, session_id)
        if blockers:
            return {
                "status": "needs_reconcile",
                "session_id": str(session_id),
                "blockers": blockers,
            }

        refs: list[dict[str, Any]] = []
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

        async with open_checkpointer_connection(settings) as connection:
            raw = AsyncPostgresSaver(connection)
            fenced = FencedCheckpointSaver(
                raw,
                database,
                run_id=run.id,
                thread_id=thread_id,
                owner_generation=generation,
                control_version=int(run.control_version),
                writer_epoch=int(head.writer_epoch) if head is not None else 0,
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
            await compiled.aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {
                    "messages": [],
                    "contract_refs": refs,
                    "active_contract_ids": [item["contract_id"] for item in refs],
                    "plan_revision": int(values.get("plan_revision") or 0) + 1,
                },
            )
    finally:
        async with database.transaction() as db:
            await release_run(db, run_id=run.id, owner=migration_owner)
    return {
        "status": "migrated",
        "session_id": str(session_id),
        "contract_refs": refs,
    }


__all__ = ["migrate_legacy_checkpoint"]
