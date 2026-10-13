"""官方图完整边界的多片段执行；没有真实模型、远端操作或虚构用户发言。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.middleware.segment import SegmentMiddleware, SegmentSchedulingError
from agent_workbench.agent.state import AgentGraphState, next_input_state
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.config.settings import get_settings
from agent_workbench.models.provider import ModelBinding
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.runtime.runner import SEGMENT_RECURSION_LIMIT, AgentRunner
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration


async def test_at_least_three_segments_keep_plan_history_and_never_replay_tools(settings):
    calls = []

    @tool
    def record_local_operation(operation_key: str) -> str:
        """本地操作替身，记录每一次实际提交。"""
        calls.append(operation_key)
        return operation_key

    model = ScriptedChatModel(
        responses=[
            *[
                tool_call("record_local_operation", {"operation_key": f"op-{index}"}, f"c-{index}")
                for index in range(11)
            ],
            final_text("本地协议演练结束"),
        ]
    )
    middleware = build_agent_middleware(
        model,
        ModelBinding(profile()),
        get_settings(),
        [record_local_operation],
        default_price_catalog(),
    )
    run = SimpleNamespace(
        id=uuid4(), session_id=uuid4(), objective="本地长图验收", config_snapshot={}
    )
    task = SimpleNamespace(thread_id=str(uuid4()))
    config = {
        "configurable": {"thread_id": task.thread_id},
        "recursion_limit": SEGMENT_RECURSION_LIMIT,
    }
    runner = AgentRunner()
    plan = [
        {
            "step_id": "s1",
            "description": "保持原计划",
            "status": "in_progress",
            "rationale": None,
            "lead_id": None,
        }
    ]
    initial = next_input_state(
        session_id=run.session_id,
        run_id=run.id,
        objective=run.objective,
        user_message=run.objective,
        previous_plan=cast(Any, plan),
    )
    yielded_ids = []
    segments = 0
    async with open_checkpointer_connection(settings) as connection:
        graph = create_agent(
            model=model,
            tools=[record_local_operation],
            middleware=middleware,
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(connection),
        )
        runtime = cast(Any, SimpleNamespace(graph=graph))
        payload = initial
        while True:
            segments += 1
            result = await graph.ainvoke(cast(Any, payload), cast(Any, config))
            snapshot = await graph.aget_state(cast(Any, config))
            assert not snapshot.next
            assert snapshot.values["plan"] == plan
            pending = {
                call["id"]
                for message in result["messages"]
                if isinstance(message, AIMessage)
                for call in message.tool_calls
            } - {
                message.tool_call_id
                for message in result["messages"]
                if isinstance(message, ToolMessage)
            }
            assert not pending
            yielded = await runner._read_segment_yield(runtime, cast(Any, run), cast(Any, task))
            if yielded is None:
                break
            yielded_ids.append(yielded["checkpoint_id"])
            payload = await runner._segment_input(
                runtime, cast(Any, run), cast(Any, task), segments + 1
            )
            assert payload["messages"] == []
            assert (
                "objective" not in payload
                and "plan" not in payload
                and "no_progress_streak" not in payload
            )
            assert segments < 6
    assert segments >= 3
    assert len(set(yielded_ids)) == len(yielded_ids)
    assert calls == [f"op-{index}" for index in range(11)]
    assert len(model.seen_message_counts) == 12
    assert sum(isinstance(message, HumanMessage) for message in result["messages"]) == 1
    assert result["messages"][-1].content == "本地协议演练结束"


async def test_yield_reopens_checkpoint_in_new_connection_without_duplicate_submission(settings):
    seen = []

    @tool
    def submit_local_once(key: str) -> str:
        """本地提交计数替身。"""
        seen.append(key)
        return key

    model = ScriptedChatModel(
        responses=[
            tool_call("submit_local_once", {"key": "first"}, "c1"),
            final_text("恢复后的答复"),
        ]
    )
    run = SimpleNamespace(id=uuid4(), session_id=uuid4(), objective="恢复协议", config_snapshot={})
    task = SimpleNamespace(thread_id=str(uuid4()))
    config = {"configurable": {"thread_id": task.thread_id}, "recursion_limit": 60}
    async with open_checkpointer_connection(settings) as connection:
        graph = create_agent(
            model=model,
            tools=[submit_local_once],
            middleware=[SegmentMiddleware(1)],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(connection),
        )
        first = await graph.ainvoke(
            cast(
                Any,
                next_input_state(
                    session_id=run.session_id,
                    run_id=run.id,
                    objective=run.objective,
                    user_message=run.objective,
                ),
            ),
            cast(Any, config),
        )
        assert first["segment_yield_reason"]
    async with open_checkpointer_connection(settings) as connection:
        graph = create_agent(
            model=model,
            tools=[submit_local_once],
            middleware=[SegmentMiddleware(1)],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(connection),
        )
        payload = await AgentRunner()._segment_input(
            cast(Any, SimpleNamespace(graph=graph)), cast(Any, run), cast(Any, task), 1
        )
        assert payload["segment_index"] == 1
        result = await graph.ainvoke(payload, cast(Any, config))
    assert seen == ["first"] and result["messages"][-1].content == "恢复后的答复"


async def test_uncommitted_or_wrong_run_yield_is_rejected():
    run = SimpleNamespace(id=uuid4())
    task = SimpleNamespace(thread_id="thread")
    values = {
        "segment_yield_reason": "completed_round_budget",
        "active_run_id": str(run.id),
        "segment_completed_rounds": 1,
        "last_completed_model_message_id": "a1",
    }

    async def pending_state(config):
        return SimpleNamespace(values=values, next=("tools",))

    with pytest.raises(SegmentSchedulingError):
        await AgentRunner()._read_segment_yield(
            cast(Any, SimpleNamespace(graph=SimpleNamespace(aget_state=pending_state))),
            cast(Any, run),
            cast(Any, task),
        )
