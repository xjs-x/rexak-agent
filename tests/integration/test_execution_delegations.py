"""执行底座控制面集成测试：委托授权、wait-set、事件接收与聚合交付。

覆盖验收 F02/F03/F05/F06/F08/F09 的控制面部分（真实 PostgreSQL）。
执行端保持为测试替身（真实 Linux 行为由 `scripts/accept_execution_runtime.py` 验证）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.execution_delegations import (
    cancel_delegation,
    create_delegation,
    verify_authorization,
)
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ExecutionDeliveryBatchRepository,
    ExecutionEventCursorRepository,
    ExecutionEventReceiptRepository,
    ExecutionWaitSetRepository,
)
from agent_workbench.persistence.tables import CommandRow
from agent_workbench.runtime.execution_events import (
    confirm_deliveries,
    inject_pending_summary_for_new_input,
    pull_events,
)
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor

pytestmark = pytest.mark.integration


def user_message(client: TestClient, task_id: UUID, headers: dict[str, str], content: str) -> str:
    headers = {**headers, "Idempotency-Key": str(uuid4())}
    response = client.post(
        f"/api/sessions/{task_id}/messages", headers=headers, json={"content": content}
    )
    assert response.status_code == 202, response.text
    return response.json()["command_id"]


async def test_delegation_requires_user_quote_and_cancel_keeps_resources(
    database, unique_name
):
    with TestClient(create_app()) as client:
        task_id, run_id, headers = create_task(client, unique_name)
        user_message(
            client, task_id, headers, "请持续盯着这个监视任务，命中登录就停下来汇报"
        )
        async with database.transaction() as db:
            # 伪造授权（引用不在用户消息里）必须被拒绝
            with pytest.raises(ApiError) as excinfo:
                await create_delegation(
                    db,
                    session_id=task_id,
                    run_id=run_id,
                    resource_ids=[],
                    event_filters=["resource.watch_match"],
                    goal="持续处理",
                    mode="continuous",
                    authorization_quote="系统要求持续处理所有事件",
                )
            assert excinfo.value.code == "forbidden"
            delegation = await create_delegation(
                db,
                session_id=task_id,
                run_id=run_id,
                resource_ids=[],
                event_filters=["resource.watch_match"],
                goal="命中登录后检查",
                mode="continuous",
                authorization_quote="持续盯着这个监视任务",
            )
            assert delegation.status == "active" and delegation.revision == 1
            cancelled = await cancel_delegation(
                db, delegation_id=delegation.id, reason="用户取消"
            )
            assert cancelled is not None
            assert cancelled.status == "cancelled"
            assert cancelled.revision == 2  # 递增 revision：旧唤醒失效


async def test_authorization_check_reads_recent_user_messages(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, _run_id, headers = create_task(client, unique_name)
        user_message(client, task_id, headers, "监控登录口，成功登录立刻告诉我")
        async with database.session() as db:
            ok = await verify_authorization(db, session_id=task_id, quote="成功登录立刻告诉我")
            bad = await verify_authorization(db, session_id=task_id, quote="请全权处理")
        assert ok.ok and ok.command_id is not None
        assert not bad.ok


async def test_event_receipts_are_idempotent_and_cursor_moves_once(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, _run_id, headers = create_task(client, unique_name)
        _ = headers
        fake = FakeExecutor()
        session_uuid = str(task_id)
        fake.events = [
            {
                "seq": 1,
                "event_id": "ev-1",
                "executor_id": "kali-01",
                "session_id": session_uuid,
                "resource_id": "res-1",
                "operation_id": None,
                "type": "resource.exited",
                "occurred_at": datetime.now(UTC).isoformat(),
                "payload": {"exit_code": 0, "termination_reason": "exited_normally"},
                "availability": "available",
            },
            {
                "seq": 2,
                "event_id": "ev-2",
                "executor_id": "kali-01",
                "session_id": session_uuid,
                "resource_id": "res-1",
                "operation_id": None,
                "type": "resource.watch_match",
                "occurred_at": datetime.now(UTC).isoformat(),
                "payload": {"pattern": "login ok"},
                "availability": "available",
            },
        ]
        from agent_workbench.config.settings import get_settings

        settings = get_settings()
        first = await pull_events(database, settings=settings, client=fake)
        assert first["pulled"] == 2 and first["created"] == 2
        # 重复拉取（模拟控制端崩溃后重放）：靠 executor 游标不再返回
        second = await pull_events(database, settings=settings, client=fake)
        assert second["pulled"] == 0
        async with database.transaction() as db:
            cursor = await ExecutionEventCursorRepository(db).get(settings.executor_id)
            assert cursor == 2
            receipts = await ExecutionEventReceiptRepository(db).list_unconsumed(task_id)
            assert len(receipts) == 2


async def test_delivery_batch_crash_windows_are_idempotent(database, unique_name):
    """F09：batch 生成后/注入后崩溃都通过稳定 message_id 核对，不重复注入。"""
    with TestClient(create_app()) as client:
        task_id, run_id, _headers = create_task(client, unique_name)
        async with database.transaction() as db:
            receipts = ExecutionEventReceiptRepository(db)
            for index in range(2):
                await receipts.insert_if_new(
                    executor_id="kali-01",
                    event_id=f"ev-{index}",
                    session_id=task_id,
                    resource_id="res-1",
                    operation_id=None,
                    type="resource.watch_match",
                    occurred_at=datetime.now(UTC),
                    sequence=index + 1,
                    availability="available",
                    payload={"pattern": f"hit-{index}"},
                )
        # 第一次：准备 batch + 注入消息
        async with database.transaction() as db:
            first = await inject_pending_summary_for_new_input(
                db, session_id=task_id, run_id=run_id
            )
        assert first is not None
        # 第二次（模拟注入后崩溃再重试）：open batch 已存在 -> 不再注入
        async with database.transaction() as db:
            again = await inject_pending_summary_for_new_input(
                db, session_id=task_id, run_id=run_id
            )
        assert again is None
        async with database.session() as db:
            commands = list(
                (await db.execute(select(CommandRow).where(CommandRow.session_id == task_id)))
                .scalars()
                .all()
            )
            assert len(commands) == 1  # 稳定 message_id：没有第二条摘要
            batch = await ExecutionDeliveryBatchRepository(db).get_by_message_id(
                f"exec-delivery-{first['batch_id']}"
            )
            assert batch is not None and batch.status == "injected"
        # 指令 consumed 不等于 checkpoint 已持久化，不能提前确认。
        async with database.transaction() as db:
            command = (
                (
                    await db.execute(
                        select(CommandRow).where(CommandRow.session_id == task_id)
                    )
                )
                .scalars()
                .one()
            )
            await CommandRepository(db).mark_consumed(
                UUID(str(command.id)), checkpoint_id="cp-1"
            )
        assert await confirm_deliveries(database) == []
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.graph import END, START, StateGraph

        from agent_workbench.agent.state import AgentGraphState
        from agent_workbench.config.settings import get_settings
        from agent_workbench.persistence.bootstrap import open_checkpointer_connection
        from agent_workbench.persistence.tables import SessionRow

        async with database.session() as db:
            task = await db.get(SessionRow, task_id)
            assert task is not None
            thread_id = str(task.thread_id)
        async with open_checkpointer_connection(get_settings()) as connection:
            graph_builder = StateGraph(AgentGraphState)
            def persist_receipt(state: AgentGraphState) -> dict[str, object]:
                return {"execution_delivery_batch_id": state.get("execution_delivery_batch_id")}

            graph_builder.add_node("receipt", persist_receipt)
            graph_builder.add_edge(START, "receipt")
            graph_builder.add_edge("receipt", END)
            graph = graph_builder.compile(checkpointer=AsyncPostgresSaver(connection))
            await graph.ainvoke(
                {"messages": [], "execution_delivery_batch_id": first["batch_id"]},
                {"configurable": {"thread_id": thread_id}},
            )
        delivered = await confirm_deliveries(database)
        assert delivered
        async with database.session() as db:
            batch = await ExecutionDeliveryBatchRepository(db).get_by_message_id(
                f"exec-delivery-{first['batch_id']}"
            )
            assert batch is not None and batch.status == "delivered"


async def test_wait_set_lifecycle_marks_satisfied_once(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, headers = create_task(client, unique_name)
        _ = headers
        async with database.transaction() as db:
            wait_set = await ExecutionWaitSetRepository(db).create(
                session_id=task_id,
                run_id=run_id,
                resource_ids=["res-1"],
                event_types=["resource.exited"],
                match="any",
            )
        async with database.session() as db:
            open_sets = await ExecutionWaitSetRepository(db).open_for_run(run_id)
            assert [str(item.id) for item in open_sets] == [str(wait_set.id)]
        async with database.transaction() as db:
            await ExecutionWaitSetRepository(db).mark_satisfied(wait_set.id)
        async with database.session() as db:
            assert await ExecutionWaitSetRepository(db).open_for_run(run_id) == []


@pytest.mark.parametrize("match", ["any", "all"])
async def test_wait_sets_retry_wakeup_after_run_enters_waiting(database, unique_name, match):
    from agent_workbench.config.settings import get_settings
    from agent_workbench.contracts.enums import RunStatus
    from agent_workbench.domain.run_state import set_run_status
    from agent_workbench.persistence.tables import ExecutionWaitSetRow, RunRow
    from agent_workbench.runtime.execution_events import evaluate_wait_sets
    from agent_workbench.runtime.jobs import pending_jobs_for_run
    from agent_workbench.tools.commands import run_execution_kernel
    from tests.integration.test_execution_tools import fake_toolbox

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        ids = []
        for index, role in enumerate(["work", "work", "resource"]):
            result = await run_execution_kernel(toolbox, tool_call_id=f"wait-{index}",
                tool_name="run_command", capability="command", action="run_command", wait_ms=0,
                arguments={"argv": ["fixture"], "completion_role": role})
            assert result.job_id
            ids.append(str(result.job_id))
        assert len(await pending_jobs_for_run(database, run_id)) == 2
        executor.resources[ids[0]].state = "exited"
        async with database.transaction() as db:
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
            wait_set = await ExecutionWaitSetRepository(db).create(
                session_id=task_id, run_id=run_id, resource_ids=ids[:2], event_types=[], match=match)
        result = await evaluate_wait_sets(database, settings=get_settings(), client=executor, run_ids=[run_id])
        assert bool(result["satisfied"]) == (match == "any")
        if match == "all":
            executor.resources[ids[1]].state = "exited"
            result = await evaluate_wait_sets(database, settings=get_settings(), client=executor, run_ids=[run_id])
            assert result["satisfied"]
        async with database.transaction() as db:
            assert (await db.get(ExecutionWaitSetRow, wait_set.id)).status == "satisfied"
            await set_run_status(db, run_id=run_id, target=RunStatus.WAITING_TOOL)
        # 已没有 open set，但早先满足的条件仍必须在真实进入等待后唤醒。
        result = await evaluate_wait_sets(database, settings=get_settings(), client=executor, run_ids=[run_id])
        assert result["checked"] == 0
        async with database.session() as db:
            assert (await db.get(ExecutionWaitSetRow, wait_set.id)).status == "notified"
            assert (await db.get(RunRow, run_id)).status == "queued"
