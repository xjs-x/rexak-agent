"""真实业务事务与离线执行端替身验证 Earth 复盘发现的工程缺陷。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import ExecutionCertainty, RunStatus
from agent_workbench.domain.run_state import set_run_status
from agent_workbench.execution.client import ExecutorProtocolError, ExecutorRejected
from agent_workbench.persistence.repositories import ExecutionResourceRepository
from agent_workbench.persistence.tables import EventRow, RunRow, ToolJobRow, ToolOperationRow
from agent_workbench.runtime.execution_events import (
    refresh_resources,
    sessions_with_execution_activity,
)
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.tools.commands import run_execution_kernel
from agent_workbench.tools.jobs import read_job_output
from tests.integration.test_execution_tools import fake_toolbox
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor
from tests.unit.test_provider_rejection import rejection_error

pytestmark = pytest.mark.integration


async def start_fixture(database, task_id, run_id, executor):
    toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
    result = await run_execution_kernel(
        toolbox,
        tool_call_id="fixture-command",
        tool_name="run_command",
        capability="command",
        action="run_command",
        arguments={"command": "fixture-only", "workspace_id": str(task_id)},
        wait_ms=0,
    )
    return toolbox, result


async def test_control_500_is_preserved_and_operation_is_not_left_accepted(database, unique_name):
    class BrokenControl(FakeExecutor):
        async def execution_sync_control(self, payload):
            raise ExecutorProtocolError("会话控制同步失败（HTTP 500）：fixture transaction error")

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        executor = BrokenControl()
        _, result = await start_fixture(database, task_id, run_id, executor)
        assert result.error and result.error.code == "executor_incompatible"
        assert "HTTP 500" in result.summary and "执行岛不可达" not in result.summary
        assert result.execution_certainty == ExecutionCertainty.NOT_APPLIED
        assert executor.envelopes == []
        async with database.session() as db:
            operation = await db.get(ToolOperationRow, result.operation_id)
            assert (
                operation is not None and operation.status == "failed" and operation.job_id is None
            )


@pytest.mark.parametrize(
    ("exit_code", "reason", "expected"),
    [
        (0, "exited_normally", "succeeded"),
        (1, "exited_normally", "failed"),
        (-15, "user_cancelled", "cancelled"),
        (None, "unknown", "unknown"),
    ],
)
async def test_background_exit_syncs_resource_operation_and_job(
    database, unique_name, exit_code, reason, expected
):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        executor = FakeExecutor()
        _, result = await start_fixture(database, task_id, run_id, executor)
        resource_id = UUID(str(result.data["resource_id"]))
        resource = executor.resources[str(resource_id)]
        resource.state, resource.exit_code, resource.termination_reason = (
            "exited",
            exit_code,
            reason,
        )
        await refresh_resources(database, settings=database.settings, client=executor)
        # 重放旧的初始回执也不能回退已确认终态。
        await start_fixture(database, task_id, run_id, executor)
        async with database.session() as db:
            operation = await db.get(ToolOperationRow, result.operation_id)
            job = await db.get(ToolJobRow, resource_id)
            view = await ExecutionResourceRepository(db).get(resource_id)
            assert operation and operation.status == expected
            assert job and job.state == expected
            assert view and view.state == "exited" and view.exit_code == exit_code
        assert len(executor.resources) == 1


async def test_historical_terminal_resource_with_stale_operation_is_rechecked(
    database, unique_name
):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        executor = FakeExecutor()
        _, result = await start_fixture(database, task_id, run_id, executor)
        resource_id = UUID(str(result.data["resource_id"]))
        resource = executor.resources[str(resource_id)]
        resource.state, resource.exit_code, resource.termination_reason = (
            "exited",
            0,
            "exited_normally",
        )
        # 模拟升级前：资源已退出，但操作和 job 留在 running。
        async with database.transaction() as db:
            view = await ExecutionResourceRepository(db).get(resource_id)
            assert view is not None
            view.state, view.exit_code = "exited", 0
        assert task_id in await sessions_with_execution_activity(database)
        await refresh_resources(database, settings=database.settings, client=executor)
        async with database.session() as db:
            operation = await db.get(ToolOperationRow, result.operation_id)
            job = await db.get(ToolJobRow, resource_id)
            assert operation and operation.status == "succeeded"
            assert job and job.state == "succeeded"


async def test_invalid_cursor_remains_a_correctable_read_error(database, unique_name):
    class RejectedOutput(FakeExecutor):
        async def execution_output(self, *args, **kwargs):
            raise ExecutorRejected("游标绑定另一个资源", status_code=422, code="invalid_cursor")

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        executor = RejectedOutput()
        toolbox, result = await start_fixture(database, task_id, run_id, executor)
        runtime = SimpleNamespace(context=SimpleNamespace(toolbox=toolbox))
        output = json.loads(
            await cast(Any, read_job_output).coroutine(
                runtime=runtime, job_id=str(result.job_id), cursor="foreign-cursor"
            )
        )
        assert output["error"]["code"] == "invalid_cursor"
        assert "无需重新运行" in output["error"]["recovery_hint"]
        assert len(executor.envelopes) == 1


async def test_provider_rejection_waits_without_internal_failure_or_request_body(
    database, unique_name
):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        runner = AgentRunner(database=database)
        async with database.transaction() as db:
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
        error = rejection_error(
            {
                "error": {"code": "data_inspection_failed", "message": "PRIVATE-MARKER"},
                "id": "req-fixture",
            }
        )
        status, detail = await runner._classify_exception(run_id, error)
        assert status == RunStatus.WAITING_USER and "内容审核" in detail
        assert "PRIVATE-MARKER" not in detail and "req-fixture" in detail
        await runner._finalize(run_id, status, detail=detail, completion_reason=None)
        async with database.session() as db:
            run = await db.get(RunRow, run_id)
            assert run and run.status == "waiting_user"
            events = (
                (
                    await db.execute(
                        select(EventRow).where(
                            EventRow.session_id == task_id, EventRow.type == "error"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1 and events[0].payload["code"] == "provider_error"
            assert events[0].payload["provider_code"] == "data_inspection_failed"
            assert "PRIVATE-MARKER" not in json.dumps(events[0].payload)
