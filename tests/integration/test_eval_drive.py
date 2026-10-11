"""评测必须限定 run，等待工具时核对，恢复/输入状态结束本次观察。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from evals.runner import _drive_until_terminal
from sqlalchemy import select

from agent_workbench.domain.run_state import create_run
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import RunRow

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stop_status", ["recovering", "waiting_user", "completed"])
async def test_eval_wakes_only_selected_run_and_stops_for_attention(
    database, unique_name, monkeypatch, stop_status
):
    from agent_workbench.runtime import jobs

    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="eval-test", name=unique_name, target_description="评测驱动", default_scope={}
        )
        task = await SessionRepository(db).create(
            project_id=project.id, title="评测", scope_snapshot={}, policy_snapshot={}
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="测试驱动",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )
        run.status = "waiting_tool"
    seen = []

    async def wake(db, *, settings, run_ids):
        seen.append(("wake", run_ids))
        async with db.transaction() as session:
            row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
            row.status = "queued"
        return {"woken": run_ids}

    class Runner:
        _db = database
        _settings = database.settings

        async def step(self, *, only_run_id):
            seen.append(("step", only_run_id))
            async with database.transaction() as session:
                row = (
                    await session.execute(select(RunRow).where(RunRow.id == run.id))
                ).scalar_one()
                row.status = stop_status

    monkeypatch.setattr(jobs, "probe_and_wake", wake)
    await asyncio.wait_for(_drive_until_terminal(cast(Any, Runner()), run.id), timeout=5)
    assert seen == [("wake", [run.id]), ("step", run.id)]
