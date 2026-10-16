"""真实业务事务中的通知恢复、暂停与委托撤销边界。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import CommandKind
from agent_workbench.domain.execution_delegations import verify_authorization
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ExecutionDelegationRepository,
    ExecutionDeliveryBatchRepository,
    ExecutionEventReceiptRepository,
)
from agent_workbench.persistence.tables import CommandRow, RunRow
from agent_workbench.runtime.execution_events import _deliver_for_delegation
from tests.integration.test_instruction_scope import create_task

pytestmark = pytest.mark.integration


async def seed_duty(db, task_id, run_id):
    duty = await ExecutionDelegationRepository(db).create(
        session_id=task_id, resource_ids=[], event_filters=["resource.watch_match"],
        goal="处理监视通知", mode="continuous", authorization_source="user_message",
        authorization_quote="持续处理监视通知", authorization_command_id=None,
        created_by_run_id=run_id,
    )
    event, _ = await ExecutionEventReceiptRepository(db).insert_if_new(
        executor_id="kali-01", event_id=str(uuid4()), session_id=task_id,
        resource_id=str(uuid4()), operation_id=None, type="resource.watch_match",
        occurred_at=datetime.now(UTC), sequence=1, availability="available", payload={},
    )
    return duty, event


@pytest.mark.parametrize("status", ["paused", "recovering"])
async def test_paused_or_recovering_does_not_strand_delivery(database, settings, unique_name, status):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            duty, event = await seed_duty(db, task_id, run_id)
            run = await db.get(RunRow, run_id)
            run.status = status
        outcome = await _deliver_for_delegation(
            database, settings=settings, session_id=task_id, delegation=duty, receipts=[event]
        )
        assert outcome is None
        async with database.session() as db:
            assert await ExecutionDeliveryBatchRepository(db).list_open_for_session(task_id) == []
            assert len(await ExecutionEventReceiptRepository(db).list_unconsumed(task_id)) == 1


async def test_cancelling_duty_releases_events_and_supersedes_queued_wakeup(
    database, settings, unique_name,
):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            duty, event = await seed_duty(db, task_id, run_id)
        outcome = await _deliver_for_delegation(
            database, settings=settings, session_id=task_id, delegation=duty, receipts=[event]
        )
        assert outcome is not None
        async with database.transaction() as db:
            await ExecutionDelegationRepository(db).cancel(duty.id, reason="取消值班，保留资源")
        async with database.session() as db:
            batch = await ExecutionDeliveryBatchRepository(db).get(UUID(outcome["batch_id"]))
            assert batch is not None and batch.status == "cancelled"
            command = await db.get(CommandRow, batch.message_command_id)
            assert command is not None and command.status == "superseded"
            assert len(await ExecutionEventReceiptRepository(db).list_unconsumed(task_id)) == 1
        assert await _deliver_for_delegation(
            database, settings=settings, session_id=task_id, delegation=duty, receipts=[event]
        ) is None


async def test_execution_event_cannot_become_user_authorization(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            await CommandRepository(db).submit(
                session_id=task_id, run_id=run_id, kind=CommandKind.MESSAGE,
                content="请持续处理监视通知", idempotency_key=str(uuid4()),
                payload={"source": "execution_events"},
            )
        async with database.session() as db:
            check = await verify_authorization(db, session_id=task_id, quote="持续处理监视通知")
            assert check.ok is False


async def test_consumed_notification_is_replayed_until_graph_receipt(database, unique_name):
    from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
    from agent_workbench.runtime.execution_events import inject_pending_summary_for_new_input

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            await seed_duty(db, task_id, run_id)
            outcome = await inject_pending_summary_for_new_input(db, session_id=task_id, run_id=run_id)
            assert outcome is not None
            await CommandRepository(db).mark_consumed(UUID(outcome["command_id"]), checkpoint_id=None)
        toolbox = SimpleNamespace(db=database, session_id=task_id)
        guard = CommandGuardMiddleware()
        update = await guard._replay_delivery(toolbox, {})
        assert update is not None
        assert update["messages"][0].id == f"exec-delivery-{outcome['batch_id']}"
        assert await guard._replay_delivery(toolbox, update) is None
        async with database.session() as db:
            rows = (await db.execute(select(CommandRow).where(
                CommandRow.session_id == task_id
            ))).scalars().all()
            assert len(rows) == 1


async def test_cancelled_duty_blocks_old_model_response_and_syncs_revocation(database, unique_name):
    from typing import Any, cast

    from langchain_core.messages import ToolMessage

    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
    from agent_workbench.tools.execution_dispatch import sync_fence
    from tests.integration.test_tool_dispatch import _build_context, _FakeRequest, _FakeRuntime
    from tests.support.executor_fixtures import FakeExecutor

    executor = FakeExecutor()
    context = await _build_context(database, unique_name, executor_stub=executor)
    toolbox = context.toolbox
    async with database.transaction() as db:
        duty, _ = await seed_duty(db, toolbox.session_id, toolbox.run_id)
        await ExecutionDelegationRepository(db).cancel(duty.id, reason="取消值班")
    request = _FakeRequest(_FakeRuntime(context), "http_request", {}, "old-duty-response")
    request.state.update(execution_delegation_id=str(duty.id), execution_delegation_revision=1)
    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="fixture", tool_call_id="old-duty-response")

    blocked = await ToolDispatchMiddleware().awrap_tool_call(cast(Any, request), handler)
    assert isinstance(blocked, ToolMessage)
    assert "值班委托已失效" in str(blocked.content)
    assert called is False
    # 同 run 中不依赖已取消委托的其他工作仍可推进。
    request.state.pop("execution_delegation_id")
    await ToolDispatchMiddleware().awrap_tool_call(cast(Any, request), handler)
    assert called is True
    await sync_fence(toolbox)
    assert executor.control_syncs[-1]["revocations"] == [
        {"delegation_id": str(duty.id), "revision": 2}
    ]


async def test_duty_and_user_message_race_create_one_followup_run(database, settings, unique_name):
    import asyncio

    from agent_workbench.contracts.enums import ACTIVE_RUN_STATUSES, RunStatus
    from agent_workbench.domain.run_state import set_run_status
    from agent_workbench.runtime.commands import submit_message

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            duty, event = await seed_duty(db, task_id, run_id)
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
            await set_run_status(db, run_id=run_id, target=RunStatus.COMPLETED)

        async def user_input():
            async with database.transaction() as db:
                return await submit_message(db, session_id=task_id, content="继续检查已有结果",
                    idempotency_key=str(uuid4()), provider_id=settings.default_provider,
                    model_name=settings.model_for_provider(settings.default_provider), config_snapshot={})

        await asyncio.gather(user_input(), _deliver_for_delegation(database, settings=settings,
            session_id=task_id, delegation=duty, receipts=[event]))
        async with database.session() as db:
            original = await db.get(RunRow, run_id)
            assert original is not None and original.status == "completed"
            active = (await db.execute(select(RunRow).where(RunRow.session_id == task_id,
                RunRow.status.in_([str(item) for item in ACTIVE_RUN_STATUSES])))).scalars().all()
            assert len(active) == 1 and active[0].id != run_id
            commands = (await db.execute(select(CommandRow).where(CommandRow.session_id == task_id))).scalars().all()
            assert len(commands) == 2 and all(row.run_id == active[0].id for row in commands)


@pytest.mark.parametrize("mode", ["once", "continuous"])
async def test_once_ends_after_handling_run_while_continuous_remains(database, settings, unique_name, mode):
    from agent_workbench.contracts.enums import RunStatus
    from agent_workbench.domain.run_state import set_run_status
    from agent_workbench.runtime.execution_events import confirm_deliveries

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            duty, event = await seed_duty(db, task_id, run_id)
            duty.mode = mode
        result = await _deliver_for_delegation(database, settings=settings, session_id=task_id,
                                              delegation=duty, receipts=[event])
        assert result is not None
        async with database.transaction() as db:
            # checkpoint 交付路径已由独立 F09 测试验证，这里聚焦处置完成的时间边界。
            await ExecutionDeliveryBatchRepository(db).mark_delivered(UUID(result["batch_id"]))
            await ExecutionDelegationRepository(db).touch_triggered(duty.id)
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
        await confirm_deliveries(database)
        async with database.session() as db:
            active = await ExecutionDelegationRepository(db).get(duty.id)
            assert active is not None and active.status == "active"
        async with database.transaction() as db:
            await set_run_status(db, run_id=run_id, target=RunStatus.COMPLETED)
        await confirm_deliveries(database)
        async with database.session() as db:
            current = await ExecutionDelegationRepository(db).get(duty.id)
            assert current is not None and current.status == ("completed" if mode == "once" else "active")
