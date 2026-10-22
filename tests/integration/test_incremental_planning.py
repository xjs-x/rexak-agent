"""轻量计划的来源、并发、提交投影与逐工具重启可靠性。"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.state import AgentGraphState, new_graph_state
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.contracts.enums import ToolEffectClass
from agent_workbench.models.provider import ModelBinding
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import ArtifactRow, ToolOperationRow
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.plan_revisions import project_committed_plan
from agent_workbench.tools.task import update_plan
from tests.integration.test_chain_planning import _call
from tests.integration.test_tool_dispatch import _build_context
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration


async def result_source(
    box: Any, call_id: str, value: bool, *, step_id: str | None = "A"
) -> dict[str, str]:
    async with box.db.transaction() as db:
        prepared = await OperationRegistry(db).prepare(
            run_id=box.run_id,
            session_id=box.session_id,
            tool_call_id=call_id,
            tool_name="fixture_result",
            arguments={"value": value},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
            context_extra={"step_id": step_id} if step_id else {},
        )
        op = prepared.row
        if op.result_snapshot is not None:
            return dict(op.result_snapshot["references"])
        stored = box.artifacts.write_bytes(
            json.dumps({"exists": value}).encode(), media_type="application/json"
        )
        artifact = await ArtifactRepository(db).create(
            artifact_id=uuid5(NAMESPACE_URL, f"fixture:{op.operation_id}"),
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            media_type=stored.media_type,
            relative_key=stored.relative_key,
            session_id=box.session_id,
            source_operation_id=op.operation_id,
            sensitivity="internal",
            availability="available",
            captured_at=stored.captured_at,
        )
        refs = {"operation_id": str(op.operation_id), "artifact_id": str(artifact.id)}
        op.status = "succeeded"
        op.result_snapshot = {"data": {"exists": value}, "references": refs}
    return refs


def step(identifier="A", status="in_progress", **kwargs):
    return {
        "step_id": identifier,
        "description": f"检查任务 {identifier}",
        "status": status,
        **kwargs,
    }


async def update(context, state, steps, call_id="plan", **extra):
    return await _call(
        update_plan,
        context,
        {
            "steps": steps,
            "expected_plan_revision": state.get("plan_revision", 0),
            "rationale": "根据结果调整工作",
            **extra,
        },
        call_id,
        state,
    )


async def test_uncommitted_artifact_is_not_a_revision_and_projection_repairs_once(
    database, unique_name, monkeypatch
):
    ctx = await _build_context(database, unique_name)
    command = await update(ctx, {}, [step()])
    config = {"configurable": {"thread_id": ctx.thread_id}}
    async with open_checkpointer_connection(database.settings) as conn:
        saver = AsyncPostgresSaver(conn)
        assert (
            await project_committed_plan(
                database,
                database.settings,
                ctx.toolbox.session_id,
                await saver.aget_tuple(cast(Any, config)),
            )
            == 0
        )
        graph = create_agent(
            model=ScriptedChatModel(responses=[final_text("保留状态")]),
            tools=[],
            state_schema=AgentGraphState,
            checkpointer=saver,
        )
        await graph.aupdate_state(cast(Any, config), cast(Any, command.update), as_node="__start__")
    # 关闭原连接后重新构图，投影只从实际提交的 checkpoint 读取。
    async with open_checkpointer_connection(database.settings) as conn:
        saved = await AsyncPostgresSaver(conn).aget_tuple(cast(Any, config))
        from agent_workbench.tools import plan_revisions

        original = plan_revisions.append_event

        async def fail_after_insert(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("模拟事件已插入但事务未提交时中断")

        with monkeypatch.context() as patch:
            patch.setattr(plan_revisions, "append_event", fail_after_insert)
            with pytest.raises(RuntimeError, match="未提交"):
                await project_committed_plan(
                    database, database.settings, ctx.toolbox.session_id, saved
                )
        counts = await asyncio.gather(
            *[
                project_committed_plan(database, database.settings, ctx.toolbox.session_id, saved)
                for _ in range(2)
            ]
        )
        assert sum(counts) == 1
        assert (
            await project_committed_plan(database, database.settings, ctx.toolbox.session_id, saved)
            == 0
        )
        # 已提交调用重放不制造新版本；没有外部操作会因投影补齐而执行。
        replay = await update(ctx, dict(command.update), [step()], expected_plan_revision=0)
        assert json.loads(replay.update["messages"][0].content)["replayed"]
    async with database.session() as db:
        assert (
            list(
                (
                    await db.execute(
                        select(ToolOperationRow).where(
                            ToolOperationRow.run_id == ctx.toolbox.run_id
                        )
                    )
                ).scalars()
            )
            == []
        )


async def test_persisted_head_conflict_rejects_stale_snapshot(database, unique_name):
    ctx = await _build_context(database, unique_name)

    async def changed_head():
        return {"plan_revision": 1}

    ctx.toolbox.planning_head_reader = changed_head
    with pytest.raises(ValueError, match="快照已过期"):
        await update(ctx, {}, [step()])


async def test_private_bundle_keeps_revision_source_without_calling_it_target_evidence(
    database, unique_name, tmp_path
):
    from agent_workbench.maintenance.run_bundle import (
        collect_run_bundle,
        verify_run_bundle,
        write_run_bundle,
    )

    ctx = await _build_context(database, unique_name)
    command = await update(ctx, {}, [step()])
    async with open_checkpointer_connection(database.settings) as connection:
        graph = create_agent(
            model=ScriptedChatModel(responses=[final_text("只读来源")]),
            tools=[],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(connection),
        )
        await graph.aupdate_state(
            {"configurable": {"thread_id": ctx.thread_id}},
            cast(Any, command.update),
            as_node="__start__",
        )
    payload = await collect_run_bundle(database, ctx.toolbox.run_id)
    reference = payload["checkpoint_projection"]["values"]["last_plan_change"]
    assert payload["audit_artifact_roles"][reference["artifact_id"]] == "plan_revision_candidate"
    directory = write_run_bundle(tmp_path / "private-bundle", payload, ctx.toolbox.artifacts)
    body = json.loads(
        (directory / "plans" / "revisions" / f"{reference['artifact_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert body["change"]["update_id"] == reference["update_id"]
    assert verify_run_bundle(directory) == []


async def test_dispatch_rejects_all_same_batch_plan_writers_before_artifacts(database, unique_name):
    from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware

    ctx = await _build_context(database, unique_name)
    batch = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "update_plan",
                "args": {"expected_plan_revision": 0, "steps": [step(identifier)]},
                "id": identifier,
                "type": "tool_call",
            }
            for identifier in ("A", "C")
        ],
    )
    graph = create_agent(
        model=ScriptedChatModel(responses=[batch, final_text("合并后重试")]),
        tools=[update_plan],
        middleware=[CommandGuardMiddleware(), ToolDispatchMiddleware()],
        state_schema=AgentGraphState,
        context_schema=RunContext,
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="检查同批竞争")]}), context=ctx
    )
    assert not result.get("plan")
    receipts = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert len(receipts) == 2 and all("全部拒绝" in item.content for item in receipts)
    async with database.session() as db:
        assert (
            list(
                (
                    await db.execute(
                        select(ArtifactRow).where(ArtifactRow.session_id == ctx.toolbox.session_id)
                    )
                ).scalars()
            )
            == []
        )


async def test_checkpoint_restart_after_each_tool_keeps_one_external_operation(
    database, unique_name
):
    ctx = await _build_context(database, unique_name)
    effects = []

    @tool
    async def fixture_once(
        runtime: ToolRuntime[RunContext], tool_call_id: Annotated[str, InjectedToolCallId]
    ) -> str:
        """本地计数，模拟可能有副作用的外部行动。"""
        refs = await result_source(runtime.context.toolbox, tool_call_id, True, step_id=None)
        effects.append(refs["operation_id"])
        return json.dumps(refs)

    def done(messages):
        return tool_call(
            "update_plan",
            {
                "expected_plan_revision": 1,
                "steps": [step(status="done")],
                "rationale": "核对已取得的结果",
            },
            "close",
        )

    model = SteppedModel(
        steps=[
            lambda _: tool_call(
                "update_plan",
                {"expected_plan_revision": 0, "steps": [step()]},
                "initial",
            ),
            lambda _: tool_call("fixture_once", {}, "stable-external-call"),
            done,
            lambda _: AIMessage(content="结果已核对"),
        ]
    )
    tools = [fixture_once, update_plan]
    config = {"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 100}
    for index in range(4):
        # 每个工具边界都关闭连接并重新构建官方图，禁止用另一条工具循环补跑。
        async with open_checkpointer_connection(database.settings) as connection:
            graph = create_agent(
                model=model,
                tools=tools,
                middleware=build_agent_middleware(
                    model,
                    ModelBinding(profile()),
                    database.settings,
                    tools,
                    default_price_catalog(),
                ),
                state_schema=AgentGraphState,
                context_schema=RunContext,
                checkpointer=AsyncPostgresSaver(connection),
                interrupt_after=["tools"],
            )
            payload = (
                cast(
                    Any,
                    new_graph_state(
                        session_id=ctx.toolbox.session_id, run_id=ctx.toolbox.run_id, objective="x"
                    ),
                )
                if index == 0
                else None
            )
            await graph.ainvoke(payload, cast(Any, config), context=ctx)
    assert len(effects) == 1
    async with open_checkpointer_connection(database.settings) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple(cast(Any, config))
        assert saved is not None
        state = saved.checkpoint["channel_values"]
        assert state["plan_revision"] == 2 and state["plan"][0]["status"] == "done"
    # 独立进程只读取已提交状态并补投影，不持有原模型或原图对象，也不调用供应商。
    import os
    import subprocess
    import sys

    child = r"""
import asyncio, json, sys
from uuid import UUID
from agent_workbench.platform_compat import configure_event_loop_policy
configure_event_loop_policy()
from agent_workbench.config.settings import get_settings
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from agent_workbench.tools.plan_revisions import project_committed_plan
async def main():
    settings = get_settings()
    database = Database(settings)
    await database.start()
    try:
        async with open_checkpointer_connection(settings) as connection:
            saved = await AsyncPostgresSaver(connection).aget_tuple({"configurable": {"thread_id": sys.argv[1]}})
        count = await project_committed_plan(database, settings, UUID(sys.argv[2]), saved)
        values = saved.checkpoint["channel_values"]
        print(json.dumps({"revision": values["plan_revision"], "projected": count, "status": values["plan"][0]["status"]}))
    finally:
        await database.close()
asyncio.run(main())
"""
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-B", "-c", child, ctx.thread_id, str(ctx.toolbox.session_id)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    restored = json.loads(completed.stdout.strip().splitlines()[-1])
    assert restored == {"revision": 2, "projected": 2, "status": "done"}
    assert (
        await project_committed_plan(database, database.settings, ctx.toolbox.session_id, saved)
        == 0
    )
    async with database.session() as db:
        assert (
            len(
                list(
                    (
                        await db.execute(
                            select(ToolOperationRow).where(
                                ToolOperationRow.run_id == ctx.toolbox.run_id
                            )
                        )
                    ).scalars()
                )
            )
            == 1
        )


async def test_local_updates_need_no_assessment_or_completion_proof(database, unique_name):
    ctx = await _build_context(database, unique_name)
    first = await update(ctx, {}, [step("A"), step("B", "pending")])
    state = dict(first.update)
    done = await update(ctx, state, [{"step_id": "A", "status": "done"}], "done")
    assert done.update["plan"][0]["status"] == "done"
    assert done.update["plan"][1] == state["plan"][1]
    assert "completion" not in done.update["plan"][0]
    state.update(done.update)
    expanded = await update(ctx, state, [step("C")], "expand", focus="独立调查方向")
    assert [s["step_id"] for s in expanded.update["plan"]] == ["A", "B", "C"]
    assert expanded.update["current_focus"]["intent"] == "独立调查方向"
    assert "work_assessment" not in expanded.update
    state.update(expanded.update)
    dropped = await update(ctx, state, [{"step_id": "B", "status": "dropped"}], "drop")
    assert dropped.update["plan"][1]["status"] == "dropped"
    state.update(dropped.update)
    # 局部更新的 done 不会提升任何 Finding，也不触发外部执行。
    async with database.session() as db:
        assert not list(
            (
                await db.execute(
                    select(ToolOperationRow).where(ToolOperationRow.run_id == ctx.toolbox.run_id)
                )
            ).scalars()
        )
    with pytest.raises(ValueError, match="版本已过期"):
        await update(ctx, state, [step("C")], "stale", expected_plan_revision=0)
    with pytest.raises(ValueError):
        await update(
            ctx, state, [{"step_id": "A", "status": "pending"}, {"step_id": "D"}], "invalid"
        )
    assert state["plan"][0]["status"] == "done", "批次失败不能部分修改原计划"


async def test_focus_can_exist_without_plan_and_shares_revision(database, unique_name):
    ctx = await _build_context(database, unique_name)
    focused = await _call(
        update_plan, ctx, {"expected_plan_revision": 0, "focus": "检查身份边界"}, "focus", {}
    )
    assert focused.update["plan"] == [] and focused.update["plan_revision"] == 1
    state = dict(focused.update)
    cleared = await _call(
        update_plan, ctx, {"expected_plan_revision": 1, "focus": ""}, "clear", state
    )
    assert cleared.update["current_focus"] is None and cleared.update["plan_revision"] == 2
