"""执行岛时间只作为 job 投影保存，领域操作失败不改写远端真实状态。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import JobState
from agent_workbench.contracts.tool import JobSnapshot
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import RunRow, ToolJobRow, ToolOperationRow
from agent_workbench.tools.remote import record_result, record_submitted
from agent_workbench.tools.runtime import Toolbox

pytestmark = pytest.mark.integration


async def test_executor_timestamps_survive_domain_result_and_repeated_sync(
    database, unique_name
) -> None:
    operation_id, job_id, run_id = uuid4(), uuid4(), uuid4()
    accepted_at = datetime.now(UTC)
    started_at = accepted_at + timedelta(seconds=2)
    finished_at = started_at + timedelta(seconds=3)
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=get_settings().owner_id,
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        work = await SessionRepository(db).create(
            project_id=project.id, title="耗时诊断", scope_snapshot={}, policy_snapshot={}
        )
        db.add(
            RunRow(
                id=run_id, session_id=work.id, status="completed", objective="测试",
                provider_id="test", model_name="scripted",
            )
        )
        db.add(
            ToolOperationRow(
                operation_id=operation_id,
                run_id=run_id,
                session_id=work.id,
                tool_call_id="call-1",
                tool_name="browser_action",
                request_digest="a" * 64,
                effect_class="side_effecting",
            )
        )

    toolbox = cast(
        Toolbox,
        SimpleNamespace(
            db=database,
            settings=SimpleNamespace(executor_id="test-executor"),
            session_id=work.id,
            run_id=run_id,
        ),
    )
    await record_submitted(toolbox, operation_id, job_id, accepted_at=accepted_at)
    await record_result(
        toolbox,
        operation_id=operation_id,
        job_id=job_id,
        status="failed",
        snapshot=JobSnapshot(
            job_id=job_id,
            operation_id=operation_id,
            kind="browser",
            state=JobState.SUCCEEDED,
            started_at=started_at,
            finished_at=finished_at,
        ),
    )
    async with database.session() as db:
        job = (await db.execute(select(ToolJobRow).where(ToolJobRow.job_id == job_id))).scalar_one()
        operation = (
            await db.execute(
                select(ToolOperationRow).where(ToolOperationRow.operation_id == operation_id)
            )
        ).scalar_one()
        assert operation.status == "failed"
        assert job.state == "succeeded"
        assert job.executor_accepted_at == accepted_at
        assert job.executor_started_at == started_at
        assert job.executor_finished_at == finished_at
