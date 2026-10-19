"""P7：旧 checkpoint plan 迁移为合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from sqlalchemy import func, select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.api.app import create_app
from agent_workbench.domain.plan_migration import migrate_legacy_checkpoint
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    CheckpointHeadRow,
    DomainIntegrationRow,
    PlanContractRow,
    RunRow,
)
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def _seed_plan(database, settings, unique_name):
    context = await context_for(database, unique_name)
    graph = StateGraph(AgentGraphState)
    graph.add_node(
        "noop",
        lambda state: {"plan_revision": int(cast(Any, state).get("plan_revision") or 0)},
    )
    graph.set_entry_point("noop")
    graph.set_finish_point("noop")
    async with open_checkpointer_connection(settings) as connection:
        saver = AsyncPostgresSaver(connection)
        compiled = graph.compile(checkpointer=saver)
        await compiled.aupdate_state(
            {"configurable": {"thread_id": context.thread_id}},
            {
                "messages": [],
                "plan_revision": 2,
                "plan": [
                    {"step_id": "s1", "description": "旧完成", "status": "done"},
                    {"step_id": "s2", "description": "旧待办", "status": "pending"},
                ],
            },
        )
        saved = await saver.aget_tuple(
            {"configurable": {"thread_id": context.thread_id}}
        )
    assert saved is not None
    async with database.transaction() as db:
        run = await db.get(RunRow, context.toolbox.run_id)
        assert run is not None
        run.status = "paused"
        run.lease_until = None
    return context, str(saved.checkpoint["id"])


async def test_migration_dry_run_then_apply_is_idempotent(database, settings, unique_name):
    context, _ = await _seed_plan(database, settings, unique_name)
    dry = await migrate_legacy_checkpoint(database, settings, context.toolbox.session_id)
    assert dry["status"] == "dry_run"
    assert [item["state"] for item in dry["contracts"]] == ["legacy_done", "proposed"]

    migrated = await migrate_legacy_checkpoint(
        database, settings, context.toolbox.session_id, apply=True
    )
    assert migrated["status"] == "migrated"
    assert len(migrated["contract_refs"]) == 2
    again = await migrate_legacy_checkpoint(
        database, settings, context.toolbox.session_id, apply=True
    )
    assert again["status"] == "already_migrated"
    async with database.session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(PlanContractRow).where(
                    PlanContractRow.session_id == context.toolbox.session_id
                )
            )
        ).scalar_one()
    assert count == 2


async def test_migration_advances_existing_accepted_head(database, settings, unique_name):
    context, accepted_id = await _seed_plan(database, settings, unique_name)
    async with database.transaction() as db:
        run = await db.get(RunRow, context.toolbox.run_id)
        assert run is not None
        db.add(
            CheckpointHeadRow(
                thread_id=context.thread_id,
                checkpoint_ns="",
                accepted_checkpoint_id=accepted_id,
                writer_epoch=0,
                owner_generation=int(run.owner_generation),
                control_version=int(run.control_version),
                accepted_at=datetime.now(UTC),
            )
        )
    migrated = await migrate_legacy_checkpoint(
        database, settings, context.toolbox.session_id, apply=True
    )
    assert migrated["status"] == "migrated"
    async with database.session() as db:
        head = await db.get(CheckpointHeadRow, (context.thread_id, ""))
        assert head is not None
    assert head.accepted_checkpoint_id != accepted_id
    async with open_checkpointer_connection(settings) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple(
            {
                "configurable": {
                    "thread_id": context.thread_id,
                    "checkpoint_id": head.accepted_checkpoint_id,
                }
            }
        )
    assert saved is not None
    assert saved.checkpoint["channel_values"].get("contract_refs")
    with TestClient(create_app()) as client:
        client.post("/api/session")
        payload = client.get(
            f"/api/sessions/{context.toolbox.session_id}/plan"
        ).json()
    assert payload["plan_source"] == "plan_contracts"
    assert len(payload["contracts"]) == 2


async def test_migration_blocks_on_pending_integration(database, settings, unique_name):
    context, _ = await _seed_plan(database, settings, unique_name)
    async with database.transaction() as db:
        db.add(
            DomainIntegrationRow(
                session_id=context.toolbox.session_id,
                entity_kind="lead",
                action="upsert",
                source_tool_call_id="pending-call",
                request_hash="a" * 64,
                status="pending",
            )
        )
        run = await db.get(RunRow, context.toolbox.run_id)
        assert run is not None
        run.status = "paused"
    dry = await migrate_legacy_checkpoint(database, settings, context.toolbox.session_id)
    assert dry["apply_eligible"] is False
    assert dry["blockers"]["pending_integrations"]
    applied = await migrate_legacy_checkpoint(
        database, settings, context.toolbox.session_id, apply=True
    )
    assert applied["status"] == "needs_reconcile"
    async with database.session() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(PlanContractRow).where(
                    PlanContractRow.session_id == context.toolbox.session_id
                )
            )
        ).scalar_one()
    assert count == 0
