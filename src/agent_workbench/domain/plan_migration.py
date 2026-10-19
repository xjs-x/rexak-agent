"""旧 checkpoint plan -> TaskContract 的一次性迁移。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from sqlalchemy import select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.enums import ACTIVE_RUN_STATUSES
from agent_workbench.domain.legacy_plan import migrate_legacy_plan
from agent_workbench.domain.plan_contracts import append_contract
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import CheckpointHeadRow, RunRow, SessionRow
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
    if run is None:
        return {"status": "no_run", "session_id": str(session_id)}
    blocked_statuses = {
        str(item) for item in ACTIVE_RUN_STATUSES if str(item) != "paused"
    }
    now = datetime.now(UTC)
    running_without_lease = (
        str(run.status) in {"queued", "running"}
        and (run.lease_until is None or run.lease_until <= now)
    )
    if str(run.status) in blocked_statuses and not running_without_lease:
        return {
            "status": "refused_active_run",
            "session_id": str(session_id),
            "run_status": str(run.status),
        }
    if (
        str(run.status) in {"paused", "running"}
        and run.lease_until is not None
        and run.lease_until > now
    ):
        return {
            "status": "refused_active_lease",
            "session_id": str(session_id),
            "run_status": str(run.status),
        }

    base_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if head is not None and head.accepted_checkpoint_id:
        base_config["configurable"]["checkpoint_id"] = head.accepted_checkpoint_id

    async with open_checkpointer_connection(settings) as connection:
        raw = AsyncPostgresSaver(connection)
        saved = await raw.aget_tuple(cast(Any, base_config))
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
                "contracts": [
                    {
                        "contract_id": item.contract_id,
                        "state": item.state,
                        "question": item.question,
                    }
                    for item in contracts
                ],
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

        fenced = FencedCheckpointSaver(
            raw,
            database,
            run_id=run.id,
            thread_id=thread_id,
            owner_generation=int(run.owner_generation),
            control_version=int(run.control_version),
            writer_epoch=int(head.writer_epoch) if head is not None else 0,
        )
        graph = StateGraph(AgentGraphState)
        graph.add_node(
            "noop",
            lambda state: {"plan_revision": int(cast(Any, state).get("plan_revision") or 0)},
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
    return {
        "status": "migrated",
        "session_id": str(session_id),
        "contract_refs": refs,
    }


__all__ = ["migrate_legacy_checkpoint"]
