"""P7：旧 checkpoint plan 迁移为合同。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from sqlalchemy import func, select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.domain.plan_migration import migrate_legacy_checkpoint
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import PlanContractRow

pytestmark = pytest.mark.integration


async def _session_with_plan(database, settings, unique_name):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="migration-test",
            name=unique_name,
            target_description="本地迁移",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id, title="旧计划", scope_snapshot={}, policy_snapshot={}
        )
        session_id = session.id
        thread_id = session.thread_id
    graph = StateGraph(AgentGraphState)
    graph.add_node(
            "noop",
            lambda state: {"plan_revision": int(cast(Any, state).get("plan_revision") or 0)},
        )
    graph.set_entry_point("noop")
    graph.set_finish_point("noop")
    async with open_checkpointer_connection(settings) as connection:
        compiled = graph.compile(checkpointer=AsyncPostgresSaver(connection))
        await compiled.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {
                "messages": [],
                "plan_revision": 2,
                "plan": [
                    {"step_id": "s1", "description": "旧完成", "status": "done"},
                    {"step_id": "s2", "description": "旧待办", "status": "pending"},
                ],
            },
        )
    return session_id


async def test_migration_dry_run_then_apply_is_idempotent(database, settings, unique_name):
    session_id = await _session_with_plan(database, settings, unique_name)
    dry = await migrate_legacy_checkpoint(database, settings, session_id)
    assert dry["status"] == "dry_run"
    assert [item["state"] for item in dry["contracts"]] == ["legacy_done", "proposed"]

    migrated = await migrate_legacy_checkpoint(database, settings, session_id, apply=True)
    assert migrated["status"] == "migrated"
    assert len(migrated["contract_refs"]) == 2
    again = await migrate_legacy_checkpoint(database, settings, session_id, apply=True)
    assert again["status"] == "already_migrated"
    async with database.session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(PlanContractRow).where(
                    PlanContractRow.session_id == session_id
                )
            )
        ).scalar_one()
    assert count == 2
