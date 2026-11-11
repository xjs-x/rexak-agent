"""真实官方图/恢复/压缩/重连与隔离 PostgreSQL；不调用真实供应商。"""

import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import add_messages
from openai import APIConnectionError
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.compaction import GuardedSummarizationMiddleware
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.context_updates import CONTEXT_UPDATE_RULE
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.agent.prompts import SYSTEM_PROMPT
from agent_workbench.agent.signals import GraphRunAborted
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import set_run_status
from agent_workbench.domain.run_timing import TIME_PROJECTION, RunTiming, load_run_timing
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.tables import ArtifactRow, EventRow, ModelCallRow, RunRow
from tests.integration.test_request_budget import context_for
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import ScriptedChatModel, final_text
from tests.unit.test_context_budget import profile
from tests.unit.test_run_timing import OBJECTIVE, START

pytestmark = pytest.mark.integration


async def _seed(context, database):
    async with database.transaction() as db:
        run = await db.get(RunRow, context.toolbox.run_id)
        assert run is not None
        run.objective = OBJECTIVE
        await set_run_status(db, run_id=run.id, target=RunStatus.RUNNING)
        await append_event(db, session_id=run.session_id, run_id=run.id,
                           event_type=EventType.RUN_STATUS, payload={"status": "running"}, occurred_at=START)
    return run


async def test_repeated_recovery_uses_first_running_event_not_creation_or_latest(database, unique_name):
    context = await context_for(database, unique_name)
    run = await _seed(context, database)
    for index in range(3):
        async with database.transaction() as db:
            for status in (RunStatus.RECOVERING, RunStatus.QUEUED, RunStatus.RUNNING):
                await set_run_status(db, run_id=run.id, target=status)
                await append_event(db, session_id=run.session_id, run_id=run.id,
                    event_type=EventType.RUN_STATUS, payload={"status": str(status)},
                    # 后来的事件即使时钟回拨也不能成为新起点。
                    occurred_at=START - timedelta(seconds=index + 1))
        async with database.session() as db:
            loaded = await load_run_timing(db, run)
        assert loaded == RunTiming(START, 4200)
    async with database.session() as db:
        # 同一 Session 的其他 Run 不构成当前 Run 的起点。
        other = RunRow(id=UUID(int=1), session_id=run.session_id, objective=OBJECTIVE)
        missing = await load_run_timing(db, other)
    assert missing.started_at is None


async def test_model_inputs_keep_system_tools_prefix_and_no_time_in_checkpoint(database, unique_name):
    context = await context_for(database, unique_name)
    run = await _seed(context, database)
    saver = InMemorySaver()
    model = SteppedModel(steps=[lambda _: final_text("自主结束")])
    original_tool = ToolMessage(content='{"ok": true}', tool_call_id="call", id="tool")
    before = deepcopy(original_tool)
    config = {"configurable": {"thread_id": context.thread_id}}
    previous_input = None
    for elapsed in (1510, 2265, 2400, 4200, 4300):
        # 每次重建 runtime/图，保留同一 checkpoint 与业务 Run。
        async with database.session() as db:
            timing = await load_run_timing(db, run)
        current = RunContext(toolbox=context.toolbox, thread_id=context.thread_id, run_timing=timing)
        graph = create_agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT,
            middleware=[ContextBuilderMiddleware(profile()),
                ModelObserverMiddleware(profile=profile(), clock=lambda seconds=elapsed: START + timedelta(seconds=seconds))],
            state_schema=AgentGraphState, context_schema=RunContext, checkpointer=saver)
        messages: list[Any] = [HumanMessage(content="继续", id=f"u-{elapsed}")]
        if previous_input is None:
            messages.extend([AIMessage(content="", id="call-msg", tool_calls=[
                {"name": "local", "args": {}, "id": "call", "type": "tool_call"}]), original_tool])
        result = await graph.ainvoke(cast(Any, {"messages": messages, "objective": OBJECTIVE}),
                                    cast(Any, config), context=current)
        sent = model.received[-1]
        assert sent[0].content == SYSTEM_PROMPT + CONTEXT_UPDATE_RULE
        assert sum(m.additional_kwargs.get("host_projection") == TIME_PROJECTION for m in sent) == 1
        assert sent[-1].additional_kwargs.get("host_projection") == TIME_PROJECTION
        assert not any(m.additional_kwargs.get("host_projection") == TIME_PROJECTION for m in result["messages"])
        assert result["messages"][-1].content == "自主结束"  # 预算未尽/耗尽都自然 END。
        assert not (await graph.aget_state(cast(Any, config))).next
        assert next(m for m in result["messages"] if m.id == "tool") == before == original_tool
        assert sum(m.additional_kwargs.get("host_projection") == "host_context_update"
                   for m in result["messages"]) == 1
        if previous_input is not None:
            assert sent[:len(previous_input) - 1] == previous_input[:-1]
            assert sent[-1].content != previous_input[-1].content
        previous_input = deepcopy(sent)
    assert "超出时长：1 分 40 秒" in str(model.received[-1][-1].content)
    assert "剩余预算：32 分 15 秒" in str(model.received[1][-1].content)


async def test_real_compaction_cannot_replace_live_timing_with_summary_estimate(database, unique_name):
    context = await context_for(database, unique_name)
    run = await _seed(context, database)
    async with database.session() as db:
        context.run_timing = await load_run_timing(db, run)
    compact = GuardedSummarizationMiddleware(
        model=ScriptedChatModel(responses=[final_text("旧估计：已经运行73分钟，预算耗尽。")]),
        trigger=("messages", 4), keep=("messages", 2))
    state = {"messages": [HumanMessage(content=f"旧观察{i}", id=f"old-{i}") for i in range(6)],
             "objective": OBJECTIVE}
    update = await compact.abefore_model(state, SimpleNamespace(context=context))
    assert update is not None and update["work_summary"]["summary_version"] == 1
    state.update({**update, "messages": add_messages(state["messages"], update["messages"])})
    model = SteppedModel(steps=[lambda _: final_text("继续技术判断")])
    graph = create_agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT,
        middleware=[ContextBuilderMiddleware(profile()), ModelObserverMiddleware(
            profile=profile(), clock=lambda: START + timedelta(seconds=2265))],
        state_schema=AgentGraphState, context_schema=RunContext)
    await graph.ainvoke(cast(Any, state), context=context)
    assert "73分钟" in str(model.received[0])
    latest = str(model.received[0][-1].content)
    assert "已运行：37 分 45 秒" in latest and "剩余预算：32 分 15 秒" in latest
    assert "历史消息、工作摘要中的时间估计不代表当前时间" in latest
    assert context.run_timing == RunTiming(START, 4200)


@pytest.mark.parametrize("stop", [None, "paused", "cancelled"])
async def test_reconnect_refreshes_each_attempt_and_keeps_control_semantics(database, unique_name, monkeypatch, stop):
    from agent_workbench.models import invocation

    context = await context_for(database, unique_name)
    context.run_timing = RunTiming(START, 4200)
    context.toolbox.settings = context.toolbox.settings.model_copy(update={"model_auto_reconnect": True})
    elapsed = 1510
    attempts = 0

    async def pause(_box, _delay):
        nonlocal elapsed
        elapsed += 100
        if stop:
            raise GraphRunAborted(stop)

    def respond(_messages):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise APIConnectionError(request=cast(Any, httpx.Request("POST", "https://fixture.invalid")))
        return final_text("自然完成")

    monkeypatch.setattr(invocation, "_reconnect_pause", pause)
    model = SteppedModel(steps=[respond])
    graph = create_agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT,
        middleware=[ContextBuilderMiddleware(profile()), ModelObserverMiddleware(
            profile=profile(), clock=lambda: START + timedelta(seconds=elapsed))],
        state_schema=AgentGraphState, context_schema=RunContext)
    if stop:
        with pytest.raises(GraphRunAborted):
            await graph.ainvoke(cast(Any, {"messages": [HumanMessage(content="任务")]}), context=context)
        assert attempts == 1
    else:
        result = await graph.ainvoke(cast(Any, {"messages": [HumanMessage(content="任务")]}), context=context)
        assert result["messages"][-1].content == "自然完成" and attempts == 3
        snapshots = [str(messages[-1].content) for messages in model.received]
        assert len(set(snapshots)) == 3
        assert all(messages[:-1] == model.received[0][:-1] for messages in model.received)
        async with database.session() as db:
            calls = list((await db.scalars(select(ModelCallRow).where(
                ModelCallRow.run_id == context.toolbox.run_id).order_by(ModelCallRow.retry_index))).all())
            events = list((await db.scalars(select(EventRow).where(
                EventRow.run_id == context.toolbox.run_id, EventRow.type == str(EventType.CONTEXT_SELECTED)
            ).order_by(EventRow.session_seq))).all())
            manifests = []
            for event in events:
                artifact = await db.get(ArtifactRow, UUID(event.payload["manifest_artifact_id"]))
                assert artifact is not None
                manifests.append(json.loads(ArtifactStore(database.settings).resolve(artifact.relative_key).read_text(encoding="utf-8")))
        assert len(calls) == 3 and len({c.logical_call_id for c in calls}) == 1
        assert [m["run_time"] for m in manifests] == snapshots
        assert all(m["budget"]["estimated_input_tokens"] <= m["budget"]["allowed_input_tokens"] for m in manifests)
