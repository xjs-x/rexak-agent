"""旧 checkpoint plan -> TaskContract 的一次性迁移。"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import Settings
from agent_workbench.domain.legacy_plan import migrate_legacy_plan
from agent_workbench.domain.plan_contracts import append_contract
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import SessionRow


async def migrate_legacy_checkpoint(
    database: Database,
    settings: Settings,
    session_id: UUID,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """读取旧 plan，生成 legacy contracts；apply 时写入业务表与 checkpoint。

    先写合同版本，再推进 checkpoint 引用；如果中途失败，重跑会复用同一
    mutation_id，不会重复追加合同。
    """
    async with database.session() as db:
        session = await db.get(SessionRow, session_id)
        if session is None:
            return {"status": "not_found", "session_id": str(session_id)}
        thread_id = session.thread_id

    async with open_checkpointer_connection(settings) as connection:
        saver = AsyncPostgresSaver(connection)
        saved = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
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
                    db, session_id=session_id, run_id=None, contract=contract
                )
                refs.append(
                    {
                        "contract_id": str(row.contract_id),
                        "contract_revision": int(row.contract_revision),
                        "content_hash": row.content_hash,
                    }
                )

        graph = StateGraph(AgentGraphState)
        graph.add_node(
            "noop",
            lambda state: {"plan_revision": int(cast(Any, state).get("plan_revision") or 0)},
        )
        graph.set_entry_point("noop")
        graph.set_finish_point("noop")
        compiled = graph.compile(checkpointer=saver)
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
