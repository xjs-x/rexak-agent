"""执行工具内核的集成测试（验收 B01/B04/B07、F01、G04 的控制面部分）。

执行端使用 `FakeExecutor`（真实 Linux 行为由隔离脚本验证）。这里检查：
- 操作身份先持久化、重放复用（不产生第二个资源）；
- 信封携带固定控制版本与 fence 序号（自填提权被拒绝是执行端的事，控制端不伪造）；
- 等待超时返回同一个 job（accepted），不重复提交；
- 能力不可用时确定失败且**不派发**；
- 控制端只读投影与控制台账同步。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import OperationStatus
from agent_workbench.persistence.repositories import (
    ExecutionResourceRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import ToolJobRow
from agent_workbench.tools.commands import run_execution_kernel
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor, fixture_capabilities

pytestmark = pytest.mark.integration


def fake_toolbox(
    database, task_id: UUID, executor: FakeExecutor, *, run_id: UUID | None = None, **overrides: Any
):
    """最小工具箱替身。`assert_control_current` 与真实 Toolbox 同语义（此处无 Runner 注入，视为通过）。"""

    async def assert_control_current() -> None:
        return None

    base = {
        "db": database,
        "run_id": run_id,
        "session_id": task_id,
        "owner": "execution-test",
        "owner_generation": 3,
        "control_version": 7,
        "settings": get_settings(),
        "executor": executor,
        "execution_fence_seq": None,
        "identity_id": None,
        "policy_revision": 1,
        "scope_revision": 1,
        "assert_control_current": assert_control_current,
    }
    base.update(overrides)
    return cast(Any, SimpleNamespace(**base))


async def test_run_command_dispatches_with_identity_and_projects(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        result = await run_execution_kernel(
            toolbox,
            tool_call_id="call-1",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "echo hi", "cwd": ".", "workspace_id": str(task_id)},
            wait_ms=1000,
        )
        assert result.status is OperationStatus.ACCEPTED
        assert len(executor.envelopes) == 1
        envelope = executor.envelopes[0]
        assert envelope["dispatch_control_version"] == 7
        assert envelope["owner_generation"] == 3
        assert envelope["session_control_seq"] == executor.fence_seq  # 用同步后的 seq
        assert envelope["arguments"]["command"] == "echo hi"
        resource_id = UUID(str(result.data["resource_id"]))
        # 控制端投影：资源 + 操作记录都已写入
        async with database.session() as db:
            projection = await ExecutionResourceRepository(db).get(resource_id)
            operation = await ToolOperationRepository(db).get(UUID(str(result.operation_id)))
            jobs = list(
                (
                    await db.execute(select(ToolJobRow).where(ToolJobRow.job_id == resource_id))
                )
                .scalars()
                .all()
            )
        assert projection is not None and projection.session_id == task_id
        assert operation is not None and operation.job_id == resource_id
        assert len(jobs) == 1


async def test_replay_reuses_operation_and_does_not_double_dispatch(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        first = await run_execution_kernel(
            toolbox,
            tool_call_id="call-replay",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "echo hi", "cwd": ".", "workspace_id": str(task_id)},
            wait_ms=0,
        )
        second = await run_execution_kernel(
            toolbox,
            tool_call_id="call-replay",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "echo hi", "cwd": ".", "workspace_id": str(task_id)},
            wait_ms=0,
        )
        assert first.operation_id == second.operation_id  # 重放复用同一逻辑操作
        assert len(executor.resources) == 1  # 只产生一个资源
        assert executor.operations[str(first.operation_id)]
        assert second.data.get("deduplicated") is True
        assert len(executor.envelopes) == 2  # 重放仍走幂等提交；执行端返回原资源


async def test_wait_timeout_returns_same_job(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        result = await run_execution_kernel(
            toolbox,
            tool_call_id="call-wait",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "sleep 999", "workspace_id": str(task_id)},
            wait_ms=10,
        )
        assert result.status is OperationStatus.ACCEPTED
        assert result.data["wait_reason"] == "wait_timeout"
        resource_id = result.data["resource_id"]
        assert resource_id in executor.resources  # 同一 job 仍在运行
        assert len(executor.envelopes) == 1  # 没有第二次提交
        assert "不要重新提交" in result.summary


async def test_disabled_capability_fails_without_dispatch(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        executor = FakeExecutor(
            capabilities_obj=fixture_capabilities(
                execution_mode="disabled", command="unavailable"
            )
        )
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        result = await run_execution_kernel(
            toolbox,
            tool_call_id="call-disabled",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "echo hi", "workspace_id": str(task_id)},
            wait_ms=0,
        )
        assert result.status is OperationStatus.FAILED
        assert result.error is not None and result.error.code == "execution_disabled"
        assert executor.envelopes == []  # 未派发任何副作用
        assert executor.resources == {}


async def test_malformed_arguments_fail_before_dispatch(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        result = await run_execution_kernel(
            toolbox,
            tool_call_id="call-bad",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "echo hi", "argv": ["echo"], "workspace_id": str(task_id)},
            wait_ms=0,
        )
        assert result.status is OperationStatus.FAILED
        assert executor.envelopes == []


async def test_identity_fields_cannot_be_injected_through_arguments(database, unique_name):
    """信封身份来自工具箱真实上下文；模型往参数里塞身份字段会被协议直接拒绝（G04 控制面）。"""
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        rejected = await run_execution_kernel(
            toolbox,
            tool_call_id="call-identity",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={
                "command": "echo hi",
                "workspace_id": str(task_id),
                # 模型试图自填身份字段：extra="forbid" 直接拒绝
                "run_id": str(uuid4()),
                "session_id": str(uuid4()),
                "dispatch_control_version": 999,
            },
            wait_ms=0,
        )
        assert rejected.status is OperationStatus.FAILED
        assert executor.envelopes == []
        # 合法参数时，身份仍由工具箱决定
        await run_execution_kernel(
            toolbox,
            tool_call_id="call-identity-ok",
            tool_name="run_command",
            capability="command",
            action="run_command",
            arguments={"command": "echo hi", "workspace_id": str(task_id)},
            wait_ms=0,
        )
        envelope = executor.envelopes[0]
        assert envelope["run_id"] == str(run_id)
        assert envelope["session_id"] == str(task_id)
        assert envelope["dispatch_control_version"] == 7
        assert executor.control_syncs[0]["run_id"] == str(run_id)
