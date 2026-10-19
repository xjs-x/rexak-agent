"""P11/P10.1 审计回归：迁移原子准入与 profile 生命周期。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from sqlalchemy import select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.domain import plan_migration
from agent_workbench.domain.coverage_checklist import CoverageProfile
from agent_workbench.domain.findings import CoverageService
from agent_workbench.domain.reports import create_report_snapshot
from agent_workbench.domain.run_state import claim_run, release_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    CheckpointHeadRow,
    CoverageProfileRow,
    ReportVersionRow,
    RunRow,
    ToolJobRow,
)
from agent_workbench.runtime.checkpoint_fencing import FencedCheckpointSaver, StaleWriterError
from tests.integration.test_coverage_v2 import _asset
from tests.integration.test_p0_evidence_provenance import _add_operation
from tests.integration.test_plan_migration import _seed_plan
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status,leased", [("running", False), ("queued", True), ("paused", True)])
async def test_dry_run_eligibility_includes_status_and_lease(database, settings, unique_name, status, leased):
    context, _ = await _seed_plan(database, settings, unique_name)
    async with database.transaction() as db:
        row = await db.get(RunRow, context.toolbox.run_id)
        row.status = status
        if leased:
            row.owner = "active-writer"
            row.lease_until = datetime.now(UTC) + timedelta(minutes=2)
    result = await plan_migration.migrate_legacy_checkpoint(database, settings, context.toolbox.session_id)
    print("dry_run_state_lease", status, leased, result)
    assert result["apply_eligible"] is False, result


async def test_migration_rechecks_run_state_inside_claim(database, settings, unique_name, monkeypatch):
    context, _ = await _seed_plan(database, settings, unique_name)
    real_claim = plan_migration.claim_run

    async def changed_before_claim(db, **kwargs):
        row = await db.get(RunRow, context.toolbox.run_id)
        row.status = "running"
        await db.flush()
        return await real_claim(db, **kwargs)

    monkeypatch.setattr(plan_migration, "claim_run", changed_before_claim)
    result = await plan_migration.migrate_legacy_checkpoint(database, settings, context.toolbox.session_id, apply=True)
    async with database.session() as db:
        row = await db.get(RunRow, context.toolbox.run_id)
        assert row.owner is None and row.lease_until is None, "Migration failed to release its ownership"
    print("state_changed_before_claim", result)
    assert result["status"] == "refused_active_run", result


async def test_migration_rejects_changed_accepted_head(database, settings, unique_name, monkeypatch):
    context, checkpoint_id = await _seed_plan(database, settings, unique_name)
    async with database.transaction() as db:
        row = await db.get(RunRow, context.toolbox.run_id)
        db.add(CheckpointHeadRow(thread_id=context.thread_id, checkpoint_ns="",
            accepted_checkpoint_id=checkpoint_id, writer_epoch=0, owner_generation=row.owner_generation,
            control_version=row.control_version, accepted_at=datetime.now(UTC)))
    original_read = plan_migration._read_accepted

    async def change_after_snapshot(settings, thread_id, head):
        saved = await original_read(settings, thread_id, head)
        graph = StateGraph(AgentGraphState)
        def noop(state: AgentGraphState):
            return {"plan_revision": state.get("plan_revision", 0)}
        graph.add_node("noop", noop)
        graph.set_entry_point("noop")
        graph.set_finish_point("noop")
        async with open_checkpointer_connection(settings) as connection:
            inner = AsyncPostgresSaver(connection)
            await graph.compile(checkpointer=inner).aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {"messages": [], "plan_revision": 99,
                 "plan": [{"step_id": "new", "description": "新增计划问题", "status": "pending"}]})
            newer = await inner.aget_tuple({"configurable": {"thread_id": thread_id}})
            assert newer is not None
        async with database.transaction() as db:
            current = await db.get(CheckpointHeadRow, (thread_id, ""))
            assert current is not None
            current.accepted_checkpoint_id = newer.checkpoint["id"]
        return saved

    monkeypatch.setattr(plan_migration, "_read_accepted", change_after_snapshot)
    result = await plan_migration.migrate_legacy_checkpoint(database, settings, context.toolbox.session_id, apply=True)
    print("head_changed_after_snapshot", result)
    assert result["status"] != "migrated", "A stale source plan was migrated after accepted head changed"


async def test_migration_claim_fences_preexisting_saver(database, settings, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        acquired, _ = await claim_run(db, run_id=context.toolbox.run_id, owner="old-writer", lease_seconds=300)
        assert acquired
    async with database.session() as db:
        row = await db.get(RunRow, context.toolbox.run_id)
    async with open_checkpointer_connection(settings) as connection:
        saver = FencedCheckpointSaver(AsyncPostgresSaver(connection), database, run_id=row.id,
            thread_id=context.thread_id, owner_generation=row.owner_generation,
            control_version=row.control_version, writer_epoch=0)
        config = cast(
            Any,
            {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}},
        )
        await saver.aput(config, empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {})
        async with database.transaction() as db:
            await release_run(db, run_id=row.id, owner="old-writer")
        async with database.transaction() as db:
            acquired, generation = await claim_run(db, run_id=row.id, owner="plan-migration:test", lease_seconds=300)
        assert acquired
        print("claim_generation", generation, "old_generation", row.owner_generation)
        with pytest.raises(StaleWriterError):
            await saver.aput(config, empty_checkpoint(), {"source": "input", "step": 1, "parents": {}}, {})


async def test_pending_job_is_found_via_operation_id(database, settings, unique_name):
    context, _ = await _seed_plan(database, settings, unique_name)
    async with database.transaction() as db:
        operation = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        db.add(ToolJobRow(job_id=uuid4(), operation_id=operation, executor_id="fixture",
                          state="running", sync_status="in_sync"))
    async with database.session() as db:
        blockers = await plan_migration._session_blockers(db, context.toolbox.session_id)
    print("pending_job_without_backpointer", blockers)
    assert blockers.get("pending_jobs"), "ToolJob.operation_id exists but optional operation.job_id is absent"


async def test_report_override_requires_registered_profile(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        db.add(_asset(context))
    result = await create_report_snapshot(database, ArtifactStore(database.settings), context.toolbox.session_id,
        coverage_profile=CoverageProfile("web", "v1", ("configuration",)), condition_fingerprint="f" * 64)
    async with database.session() as db:
        report = (await db.execute(select(ReportVersionRow).where(
            ReportVersionRow.session_id == context.toolbox.session_id, ReportVersionRow.version == result.version))).scalar_one()
        profiles = (await db.execute(select(CoverageProfileRow).where(
            CoverageProfileRow.session_id == context.toolbox.session_id))).scalars().all()
    checklist = report.captured_snapshot["body"]["coverage"]["checklist"]
    print("unregistered_report_override", checklist, "profile_rows", len(profiles))
    assert checklist.get("error") == "profile_not_registered", checklist


async def test_pre_p10_profile_hash_is_backfilled_or_compatible(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        row = CoverageProfileRow(session_id=context.toolbox.session_id, profile_id="web", profile_version="v1",
            condition_fingerprint="f" * 64, definition_hash="", categories=["configuration", "auth"],
            include_parameter_cells=True, include_anonymous=True)
        db.add(row)
        await db.flush()
        repeated = await CoverageService(db).register_profile(context.toolbox.session_id,
            profile=CoverageProfile("web", "v1", ("configuration", "auth")), condition_fingerprint="f" * 64)
        assert repeated.profile_row_id == row.profile_row_id


async def test_blocker_added_during_claim_is_rechecked_and_lease_released(database, settings, unique_name, monkeypatch):
    from agent_workbench.persistence.tables import DomainIntegrationRow, PlanContractRow
    context, _ = await _seed_plan(database, settings, unique_name)
    real_claim = plan_migration.claim_run

    async def claim_with_new_blocker(db, **kwargs):
        result = await real_claim(db, **kwargs)
        db.add(DomainIntegrationRow(session_id=context.toolbox.session_id, entity_kind="lead", action="audit",
            source_tool_call_id="arrived-during-claim", request_hash="a" * 64, status="pending"))
        await db.flush()
        return result

    monkeypatch.setattr(plan_migration, "claim_run", claim_with_new_blocker)
    result = await plan_migration.migrate_legacy_checkpoint(database, settings, context.toolbox.session_id, apply=True)
    assert result["status"] == "needs_reconcile", result
    async with database.session() as db:
        row = await db.get(RunRow, context.toolbox.run_id)
        assert row.owner is None and row.lease_until is None
        contracts = (await db.execute(select(PlanContractRow).where(
            PlanContractRow.session_id == context.toolbox.session_id))).scalars().all()
        assert not contracts


async def test_migration_lease_blocks_scheduler_claim(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        acquired, _ = await claim_run(db, run_id=context.toolbox.run_id, owner="plan-migration:fixture", lease_seconds=300)
    assert acquired
    async with database.transaction() as db:
        acquired, _ = await claim_run(db, run_id=context.toolbox.run_id, owner="runner:fixture", lease_seconds=300)
    assert not acquired
    async with database.transaction() as db:
        await release_run(db, run_id=context.toolbox.run_id, owner="plan-migration:fixture")


async def test_default_unregistered_report_is_unavailable_and_does_not_register(database, unique_name):
    context = await context_for(database, unique_name)
    result = await create_report_snapshot(database, ArtifactStore(database.settings), context.toolbox.session_id)
    async with database.session() as db:
        report = (await db.execute(select(ReportVersionRow).where(
            ReportVersionRow.session_id == context.toolbox.session_id, ReportVersionRow.version == result.version))).scalar_one()
        profiles = (await db.execute(select(CoverageProfileRow).where(
            CoverageProfileRow.session_id == context.toolbox.session_id))).scalars().all()
    assert report.captured_snapshot["body"]["coverage"]["checklist"]["error"] == "profile_not_registered"
    assert not profiles


async def test_concurrent_same_profile_registration_is_idempotent(database, unique_name):
    import asyncio
    context = await context_for(database, unique_name)
    barrier = asyncio.Barrier(2)

    async def register():
        async with database.transaction() as db:
            class AfterSelectBarrier:
                async def execute(self, statement):
                    result = await db.execute(statement)
                    await barrier.wait()
                    return result
                def add(self, row):
                    db.add(row)
                async def flush(self):
                    await db.flush()
            row = await CoverageService(cast(Any, AfterSelectBarrier())).register_profile(
                context.toolbox.session_id,
                profile=CoverageProfile("web", "v1", ("configuration", "auth")), condition_fingerprint="f" * 64)
            return row.profile_row_id

    results = await asyncio.gather(register(), register(), return_exceptions=True)
    print("concurrent_profile_results", [type(item).__name__ for item in results])
    assert not any(isinstance(item, BaseException) for item in results), [type(item).__name__ for item in results]
    assert results[0] == results[1]
