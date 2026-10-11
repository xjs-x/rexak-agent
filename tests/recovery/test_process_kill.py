"""P8 进程级演练：运行所有者被杀后，只在租约到期时进入恢复态。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import create_run, set_run_status
from agent_workbench.observability.events import read_events_after
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import RunRow
from agent_workbench.runtime.recovery import recover_stale_runs

pytestmark = [pytest.mark.integration, pytest.mark.recovery]

_CHILD = """
import asyncio
import sys
from pathlib import Path
from uuid import UUID
from agent_workbench.platform_compat import configure_event_loop_policy
from agent_workbench.persistence.db import Database
from agent_workbench.domain.run_state import claim_run

configure_event_loop_policy()

async def main():
    db = Database()
    await db.start()
    try:
        async with db.transaction() as session:
            acquired, generation = await claim_run(
                session, run_id=UUID(sys.argv[1]), owner='killed-owner', lease_seconds=30
            )
            if not acquired:
                raise RuntimeError('无法取得测试 run 所有权')
        Path(sys.argv[2]).write_text(str(generation), encoding='utf-8')
    finally:
        await db.close()
    await asyncio.sleep(3600)

asyncio.run(main())
"""


async def _wait_for_claim(process: subprocess.Popen[bytes], ready: Path) -> None:
    for _ in range(100):
        if ready.is_file():
            return
        if process.poll() is not None:
            output = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            pytest.fail(f"测试所有者提前退出：{output}")
        await asyncio.sleep(0.05)
    pytest.fail("测试所有者未在 5 秒内取得租约")


async def test_killed_owner_enters_recovery_once_after_lease_expiry(
    database, unique_name, tmp_path: Path
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="process-kill-test",
            name=unique_name,
            target_description="本地恢复演练",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id,
            title="进程被杀",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="验证进程崩溃恢复",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )
        await set_run_status(db, run_id=run.id, target=RunStatus.RUNNING)
        run_id: UUID = run.id
        session_id: UUID = task.id

    ready = tmp_path / "claimed.txt"
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD, str(run_id), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        await _wait_for_claim(child, ready)
        async with database.session() as db:
            current = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
            assert current.owner == "killed-owner"
            assert current.status == "running"
        child.kill()
        await asyncio.to_thread(child.wait, 5)
        # 租约尚未到期时不抢走已提交的所有权。
        assert await recover_stale_runs(database, only_run_id=run_id) == []
        async with database.transaction() as db:
            current = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
            assert current.lease_until is not None
            current.lease_until = current.lease_until.replace(year=2000)
        assert await recover_stale_runs(database, only_run_id=run_id) == [run_id]
        assert await recover_stale_runs(database, only_run_id=run_id) == []
        async with database.session() as db:
            current = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
            events = await read_events_after(db, session_id=session_id)
        assert current.status == "recovering"
        assert current.owner is None
        assert len([event for event in events if event.type == EventType.RUN_STATUS]) == 1
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait, 5)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()
