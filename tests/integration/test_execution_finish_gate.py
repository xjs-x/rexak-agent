"""finish_gate 对资源/等待/值班的区分（验收 G01/F01）。

- 未满足的等待条件 -> wait_tool（不是"继续探索"，也不是"已完成"）；
- 有限任务（命令/脚本）未完成 -> wait_tool，**不能**改称"保留资源"放行；
- 终端/监视器等长期资源本身不阻塞完成，但必须出现在"保留资源"里；
- 有效持续委托 -> 报告显示"持续值班中"。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.persistence.repositories import (
    ExecutionResourceRepository,
    ExecutionWaitSetRepository,
)
from agent_workbench.runtime.stopping import FinishGate
from tests.integration.test_instruction_scope import create_task

pytestmark = pytest.mark.integration


def proposal() -> dict[str, object]:
    return {
        "reason": "goal_answered",
        "goal_answer": "目标已回答",
        "stop_rationale": "没有剩余可推进方向",
        "evidence_refs": [],
        "missing_conditions": [],
    }


async def test_open_wait_set_blocks_completion(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        async with database.transaction() as db:
            wait_set = await ExecutionWaitSetRepository(db).create(
                session_id=task_id,
                run_id=run_id,
                resource_ids=["res-1"],
                event_types=["resource.exited"],
                match="any",
            )
        async with database.session() as db:
            decision = await FinishGate(db).evaluate(
                session_id=task_id, proposal=proposal(), plan=[]
            )
        assert decision.decision == "wait_tool"
        assert str(wait_set.id) in " ".join(decision.blocking_objects["open_wait_sets"])


async def test_finite_command_resource_blocks_but_terminal_does_not(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        async with database.transaction() as db:
            repository = ExecutionResourceRepository(db)
            await repository.upsert(
                resource_id=uuid4(),
                session_id=task_id,
                executor_id="kali-01",
                kind="command",
                state="running",
                purpose="后台扫描",
                origin_run_id=run_id,
            )
        async with database.session() as db:
            decision = await FinishGate(db).evaluate(
                session_id=task_id, proposal=proposal(), plan=[]
            )
        assert decision.decision == "wait_tool"  # 有限任务未完成：不能当作"保留资源"放行
        assert any("command" in item for item in decision.blocking_objects["unresolved_jobs"])


async def test_terminal_and_duty_are_reported_without_blocking(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, headers = create_task(client, unique_name)
        # 用户明确要求持续处理 -> 才能建立委托
        headers = {**headers, "Idempotency-Key": str(uuid4())}
        message = client.post(
            f"/api/sessions/{task_id}/messages",
            headers=headers,
            json={"content": "请持续盯着终端里的输出，一旦出现登录成功就汇报"},
        )
        assert message.status_code == 202
        command_id = UUID(message.json()["command_id"])
        async with database.transaction() as db:
            # 指令已消费（模拟进入一致边界后），否则 finish_gate 会先因待处理指令拒绝
            from agent_workbench.persistence.repositories import CommandRepository

            await CommandRepository(db).mark_consumed(command_id, checkpoint_id="cp-test")
        terminal_id = uuid4()
        async with database.transaction() as db:
            await ExecutionResourceRepository(db).upsert(
                resource_id=terminal_id,
                session_id=task_id,
                executor_id="kali-01",
                kind="terminal",
                state="running",
                interaction_state="available",
                mode="interactive",
                purpose="交互式测试终端",
                origin_run_id=run_id,
            )
            from agent_workbench.domain.execution_delegations import create_delegation

            await create_delegation(
                db,
                session_id=task_id,
                run_id=run_id,
                resource_ids=[str(terminal_id)],
                event_filters=["resource.watch_match"],
                goal="登录成功时汇报",
                mode="continuous",
                authorization_quote="持续盯着终端里的输出",
            )
        async with database.session() as db:
            decision = await FinishGate(db).evaluate(
                session_id=task_id, proposal=proposal(), plan=[]
            )
        # 终端与委托都不阻塞阶段完成，但必须出现在报告里
        assert decision.decision in {"complete", "wait_user"}
        assert any(
            item.startswith(f"terminal:{terminal_id}") for item in decision.retained_resources
        )
        assert any(item.startswith("duty:") for item in decision.retained_resources)
        assert "持续值班中" in decision.duty_note
        assert decision.as_metadata()["retained_resources"]


async def test_missing_duty_says_retained_but_not_served(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, _run_id, _headers = create_task(client, unique_name)
        async with database.session() as db:
            decision = await FinishGate(db).evaluate(
                session_id=task_id, proposal=proposal(), plan=[]
            )
        assert "未委托" in decision.duty_note


async def test_command_listener_role_survives_refresh_and_does_not_block(database, unique_name):
    """同样由命令启动，保留监听能力与待完成扫描使用不同完成语义。"""
    from agent_workbench.runtime.jobs import pending_jobs_for_run
    from agent_workbench.tools.commands import run_execution_kernel
    from tests.integration.test_execution_tools import fake_toolbox
    from tests.support.executor_fixtures import FakeExecutor

    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        result = await run_execution_kernel(
            fake_toolbox(database, task_id, FakeExecutor(), run_id=run_id),
            tool_call_id="listener", tool_name="run_command", capability="command",
            action="run_command", wait_ms=0,
            arguments={"argv": ["fixture-listener"], "completion_role": "resource"},
        )
        assert result.job_id is not None
        async with database.transaction() as db:
            repo = ExecutionResourceRepository(db)
            await repo.upsert(resource_id=result.job_id, session_id=task_id,
                              executor_id="kali-01", kind="command", state="running",
                              detail={"notes": ["刷新状态"]})
            resource = await repo.get(result.job_id)
            assert resource is not None and resource.detail["completion_role"] == "resource"
        assert await pending_jobs_for_run(database, run_id) == []
        async with database.session() as db:
            decision = await FinishGate(db).evaluate(session_id=task_id, proposal=proposal(), plan=[])
        assert decision.decision in {"complete", "wait_user"}
        assert any(str(result.job_id) in item for item in decision.retained_resources)
        from agent_workbench.contracts.enums import RunStatus
        from agent_workbench.domain.run_state import set_run_status
        from agent_workbench.runtime.jobs import _wake_run

        async with database.transaction() as db:
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
            await set_run_status(db, run_id=run_id, target=RunStatus.WAITING_TOOL)
        assert await _wake_run(database, run_id, summary="有限工作已结束，保留监听")
