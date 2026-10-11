"""暂停是真正可继续的状态；旧 checkpoint 不能堵住后续指令。"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.signals import GraphRunAborted
from agent_workbench.agent.state import AgentGraphState, next_input_state
from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import CommandKind, CommandStatus, RunStatus
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.domain.run_state import claim_run, set_run_status
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import CommandRepository
from agent_workbench.persistence.tables import CommandRow, RunRow, SessionRow
from agent_workbench.runtime.runner import AgentRunner
from tests.integration.test_instruction_scope import create_task
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

pytestmark = pytest.mark.integration


async def toolbox_for(database, task_id, run_id):
    async with database.transaction() as db:
        await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
        _, generation = await claim_run(db, run_id=run_id, owner="pause-test", lease_seconds=120)
        task = await db.get(SessionRow, task_id)
    return SimpleNamespace(
        db=database,
        run_id=run_id,
        session_id=task_id,
        owner="pause-test",
        owner_generation=generation,
        scope=ScopePolicy(ScopeSnapshot.model_validate(task.scope_snapshot)),
        scope_revision=task.scope_snapshot["revision"],
        control_version=0,
        checkpoint_id=None,
    )


async def test_pause_consumption_commits_and_new_message_wakes_same_run(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, headers = create_task(client, unique_name)
        toolbox = await toolbox_for(database, task_id, run_id)
        async with database.transaction() as db:
            receipt = await CommandRepository(db).submit(
                session_id=task_id,
                run_id=run_id,
                kind=CommandKind.PAUSE,
                content="",
                idempotency_key=str(uuid4()),
                priority=10,
                control_version=0,
            )
        guard = CommandGuardMiddleware()
        runtime = SimpleNamespace(context=RunContext(toolbox=cast(Any, toolbox)))
        with pytest.raises(GraphRunAborted):
            await guard.abefore_model({}, runtime)
        async with database.session() as db:
            command = await db.get(CommandRow, receipt.row.id)
            assert command.status == str(CommandStatus.CONSUMED)
        runner = AgentRunner(database=database)
        await runner._finalize(run_id, RunStatus.PAUSED, detail="用户暂停", completion_reason=None)
        headers["Idempotency-Key"] = str(uuid4())
        response = client.post(
            f"/api/sessions/{task_id}/messages",
            headers=headers,
            json={"content": "测试 http://next.example.test/"},
        )
        assert response.status_code == 202, response.text
        assert response.json()["run_id"] == str(run_id)
        async with database.session() as db:
            run = await db.get(RunRow, run_id)
            task = await db.get(SessionRow, task_id)
            assert run.status == "queued"
            assert (
                run.config_snapshot["restart_checkpoint_on_input"] == response.json()["command_id"]
            )
            assert ScopePolicy(ScopeSnapshot.model_validate(task.scope_snapshot)).is_allowed(
                "https://next.example.test:8443/"
            )
        toolbox = await toolbox_for(database, task_id, run_id)
        update = await guard.abefore_model(
            {}, SimpleNamespace(context=RunContext(toolbox=cast(Any, toolbox)))
        )
        assert update is not None and "next.example.test" in str(update["messages"][0].content)


@pytest.mark.parametrize("reuse_run", [True, False])
async def test_postgres_pending_tools_are_closed_without_replaying_when_user_changes_direction(
    database, settings, unique_name, reuse_run
):
    calls = []

    @tool
    def old_action() -> str:
        """记录旧工具是否真的被执行。"""
        calls.append("executed")
        return "成功"

    with TestClient(create_app()) as client:
        task_id, old_id, headers = create_task(client, unique_name)
        toolbox = await toolbox_for(database, task_id, old_id)
        async with database.session() as db:
            task = await db.get(SessionRow, task_id)
        model = ScriptedChatModel(
            responses=[tool_call("old_action", {}, "old-call"), final_text("已接受新的输入")]
        )
        async with open_checkpointer_connection(settings) as connection:
            graph = create_agent(
                model,
                tools=[old_action],
                state_schema=AgentGraphState,
                context_schema=RunContext,
                checkpointer=AsyncPostgresSaver(connection),
                middleware=[CommandGuardMiddleware()],
                interrupt_before=["tools"],
            )
            config: RunnableConfig = {"configurable": {"thread_id": task.thread_id}}
            await graph.ainvoke(
                cast(
                    Any,
                    next_input_state(
                        session_id=task_id, run_id=old_id, objective="旧目标", user_message="旧目标"
                    ),
                ),
                config,
                context=RunContext(toolbox=cast(Any, toolbox)),
            )
            assert (await graph.aget_state(config)).next == ("tools",)
            await AgentRunner(database=database)._finalize(
                old_id,
                RunStatus.PAUSED if reuse_run else RunStatus.FAILED,
                detail="中断",
                completion_reason=None,
            )
            headers["Idempotency-Key"] = str(uuid4())
            response = client.post(
                f"/api/sessions/{task_id}/messages",
                headers=headers,
                json={"content": "对 http://fresh.example.test/ 进行测试"},
            )
            assert response.status_code == 202, response.text
            run_id = UUID(response.json()["run_id"])
            assert (run_id == old_id) is reuse_run
            toolbox = await toolbox_for(database, task_id, run_id)
            async with database.session() as db:
                run = await db.get(RunRow, run_id)
            runner = AgentRunner(database=database)
            payload = await runner._segment_input(
                cast(Any, SimpleNamespace(graph=graph)), run, task, 1
            )
            assert (await graph.aget_state(config)).next == ()
            assert len(payload["messages"]) == 1 and isinstance(payload["messages"][0], ToolMessage)
            result = await graph.ainvoke(
                payload, config, context=RunContext(toolbox=cast(Any, toolbox))
            )
            assert result["messages"][-1].content == "已接受新的输入"
            assert any(
                isinstance(m, HumanMessage) and "fresh.example.test" in str(m.content)
                for m in result["messages"]
            )
            assert calls == [], "新指令不能偷偷重放暂停前的工具"
            assert (
                sum(
                    isinstance(m, ToolMessage) and m.tool_call_id == "old-call"
                    for m in result["messages"]
                )
                == 1
            )


async def test_pause_during_model_wait_cancels_wait_and_releases_worker(
    database, unique_name, monkeypatch
):
    with TestClient(create_app()) as client:
        _, run_id, headers = create_task(client, unique_name)
        started = asyncio.Event()
        stopped = asyncio.Event()
        runner = AgentRunner(database=database)

        async def waiting(*_args):
            await runner._set_status(run_id, RunStatus.RUNNING)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(runner, "_run_segments", waiting)
        driving = asyncio.create_task(runner._drive(run_id))
        await asyncio.wait_for(started.wait(), timeout=5)
        response = client.post(
            f"/api/runs/{run_id}/pause", headers={**headers, "Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 202, response.text
        assert await runner._pausing_runs([run_id]) == [run_id]
        driving.cancel()
        outcome = await asyncio.wait_for(driving, timeout=5)
        assert outcome is not None
        assert stopped.is_set() and outcome.status is RunStatus.PAUSED
        async with database.session() as db:
            run = await db.get(RunRow, run_id)
            assert run.status == "paused" and run.owner is None
            assert await CommandRepository(db).list_pending(run.session_id) == []


async def test_old_frontend_empty_scope_is_automatic_and_explicit_strict_is_preserved(
    database, unique_name
):
    with TestClient(create_app()) as client:
        task_id, run_id, headers = create_task(
            client, unique_name, objective="测试 http://legacy-client.example.test/", scope={}
        )
        async with database.session() as db:
            task = await db.get(SessionRow, task_id)
            assert task.scope_snapshot["mode"] == "instruction"
        # 复现上一版 API 把旧客户端的空 scope 默认保存成 scoped 的实际数据。
        async with database.transaction() as db:
            task = await db.get(SessionRow, task_id)
            task.scope_snapshot = {"mode": "scoped", "revision": 1, "rules": []}
            run = await db.get(RunRow, run_id)
            run.config_snapshot = {"created_from_session_create": True}
        headers["Idempotency-Key"] = str(uuid4())
        response = client.post(
            f"/api/sessions/{task_id}/messages",
            headers=headers,
            json={"content": "对这个网站进行测试 http://normal.example.test/"},
        )
        assert response.status_code == 202, response.text
        async with database.session() as db:
            task = await db.get(SessionRow, task_id)
            assert task.scope_snapshot["mode"] == "instruction"
            assert ScopePolicy(ScopeSnapshot.model_validate(task.scope_snapshot)).is_allowed(
                "http://normal.example.test/"
            )
