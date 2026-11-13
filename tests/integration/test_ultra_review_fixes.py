"""Targeted regression for the six Ultra review findings; isolated DB/Mock only."""

import asyncio
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.agent_delegations import reconcile_inactive_parents
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    RunRow,
    ToolJobRow,
    ToolOperationRow,
)
from agent_workbench.runtime.runner import AgentRunner
from tests.integration.test_ultra_delegations import make_manager

pytestmark = pytest.mark.integration


async def seed(manager, db, *, count=1, cancel=False):
    ids = [uuid4() for _ in range(count)]
    async with db.transaction() as session:
        for index, item in enumerate(ids):
            session.add(AgentDelegationRow(
                id=item, parent_session_id=manager.session_id, parent_run_id=manager.run_id,
                parent_tool_call_id=f"review-{index}", request_digest="0" * 64,
                objective="isolated fixture", context_refs=[], authorization_snapshot={},
                state="unknown", summary="", cancel_requested=cancel,
                diagnostic="request_not_observed_no_automatic_resubmit",
                updated_at=datetime.now(UTC) - timedelta(days=1),
            ))
    return ids


async def test_node_review_barriers_and_sdk_drain(database, settings, tmp_path):
    node = shutil.which("node")
    assert node
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
    env["REXAK_TEST_DATABASE_URL"] = make_url(settings.database_url).set(
        drivername="postgresql").render_as_string(hide_password=False)
    output = tmp_path / "node-reliability.log"
    with output.open("wb") as handle:
        result = await asyncio.to_thread(subprocess.run,
            [node, "--import", "tsx", "--test", "test/reliability.test.ts", "test/pi-runtime.test.ts"],
            cwd=REPO_ROOT / "worker", env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=40,
        )
    log = output.read_text(encoding="utf-8")
    assert result.returncode == 0, log
    assert "skipped 0" in log


async def test_unchanged_unknown_rotates_and_65th_cancel_is_delivered(database, settings, monkeypatch):
    from agent_workbench.execution.a2a_worker import A2AWorkerClient

    manager = await make_manager(database, settings)
    ids = await seed(manager, database, count=64)

    async def absent(*args):
        return None

    delivered = []

    async def cancelled(self, request_id, task_id):
        delivered.append(request_id)
        return

    monkeypatch.setattr(A2AWorkerClient, "lookup", absent)
    monkeypatch.setattr(A2AWorkerClient, "cancel", cancelled)
    # Avoid reading user's service file; explicit isolated token.
    settings = settings.model_copy(update={"ultra_worker_token": "isolated-review-token-32-characters"})
    manager.settings = settings
    first = await manager.read(ids[0])
    await manager.refresh(ids[0])
    second = await manager.read(ids[0])
    await manager.refresh(ids[0])
    third = await manager.read(ids[0])
    assert first.updated_at < second.updated_at < third.updated_at
    assert third.state == "unknown"
    pending = await make_manager(database, settings)
    last = (await seed(pending, database, cancel=True))[0]
    await reconcile_inactive_parents(database, settings)
    assert last in delivered
    assert (await pending.read(last)).state == "cancelled"


async def test_worker_credential_error_still_cancels_real_main_job_path(database, settings, monkeypatch):
    from agent_workbench.config import ultra_worker
    from agent_workbench.runtime import execution_control
    from agent_workbench.runtime import runner as runner_module

    manager = await make_manager(database, settings)
    record = (await seed(manager, database))[0]
    operation_id, job_id = uuid4(), uuid4()
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        assert run
        run.status = "cancelling"
        db.add(ToolOperationRow(operation_id=operation_id, job_id=job_id, run_id=run.id,
            session_id=run.session_id, tool_call_id="active-main-job", tool_name="run_command",
            request_digest="0" * 64, effect_class="read_only", status="running"))
        await db.flush()
        db.add(ToolJobRow(job_id=job_id, operation_id=operation_id, executor_id=settings.executor_id, state="running"))

    def broken_config(*args):
        raise OSError("Injected DPAPI failure")

    monkeypatch.setattr(ultra_worker, "read_worker_config", broken_config)
    cancelled_jobs = []

    class Executor:
        async def start(self):
            pass

        async def cancel(self, target, operation):
            cancelled_jobs.append(target)
            return SimpleNamespace(state="cancelled", process_confirmed_exited=True)

        async def close(self):
            pass

    async def terminated(*args):
        return True

    monkeypatch.setattr(runner_module, "HttpExecutorClient", lambda *_args, **_kwargs: Executor())
    monkeypatch.setattr(execution_control, "terminate_session_resources", terminated)
    runner = AgentRunner(database=database, settings=settings.model_copy(update={
        "ultra_worker_token": "", "job_wakeup_interval_seconds": 0.01}), owner="test-owner")
    outcome = await runner.step(only_run_id=manager.run_id)
    assert job_id in cancelled_jobs, outcome
    assert outcome and outcome.status == RunStatus.CANCELLING
    assert (await manager.read(record)).state == "cancel_requested"
    diagnostic = (await manager.read(record)).diagnostic
    assert diagnostic is not None and "OSError" in diagnostic


async def test_cancellation_slot_rotates_past_unconfirmed_ultra(database, settings, monkeypatch):
    older = await make_manager(database, settings)
    newer = await make_manager(database, settings)
    now = datetime.now(UTC)
    async with database.transaction() as db:
        old = await db.get(RunRow, older.run_id)
        new = await db.get(RunRow, newer.run_id)
        assert old and new
        for row in (old, new):
            row.status = "cancelling"
        old.created_at, old.updated_at = now - timedelta(days=3), now - timedelta(hours=1)
        new.created_at, new.updated_at = now, now - timedelta(hours=2)
    runner = AgentRunner(database=database, settings=settings.model_copy(update={
        "job_wakeup_interval_seconds": 0.01}), owner="test-owner", production_scan=True)
    attempts = []

    async def cancel(run):
        attempts.append(run.id)
        return run.id == older.run_id

    monkeypatch.setattr(runner, "_cancel_active_jobs", cancel)
    for _ in range(3):
        candidates = await runner._candidate_runs(only_cancelling=True, limit=1)
        assert candidates
        await runner.step(only_run_id=candidates[0].id)
        if older.run_id in attempts:
            break
    assert attempts[:2] == [newer.run_id, older.run_id]
    async with database.session() as db:
        assert await db.scalar(select(RunRow.status).where(RunRow.id == older.run_id)) == "cancelled"
