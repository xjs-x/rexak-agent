"""不完整模型响应的恢复：通过真实 `create_agent` 装配验证，坏响应零派发。

覆盖 Earth 形态（length 截断 + 空正文 + 无工具）、恢复耗尽、明确拒绝、
恢复间隙暂停、有效工具与破损工具混合、重启后恢复计数与正常聊天。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelResponse
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.agent.middleware.response_recovery import (
    ModelResponseRefused,
    ModelResponseUnavailable,
    ResponseRecoveryMiddleware,
)
from agent_workbench.agent.signals import GraphRunAborted
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.tables import EventRow, ModelCallRow
from tests.integration.test_request_budget import context_for
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration


def _truncated() -> AIMessage:
    """Earth 形态：length 截断、正文为空、没有工具调用。"""
    return AIMessage(content="", response_metadata={"finish_reason": "length"})


def _empty() -> AIMessage:
    return AIMessage(content="")


def _refused() -> AIMessage:
    return AIMessage(content="无法协助", response_metadata={"finish_reason": "content_filter"})


def _middlewares(context: RunContext, *, max_recoveries: int = 2) -> list[Any]:
    return [
        ResponseRecoveryMiddleware(max_recoveries=max_recoveries),
        ContextBuilderMiddleware(profile()),
        ModelObserverMiddleware(),
    ]


async def _calls(database, run_id) -> list[ModelCallRow]:
    async with database.session() as db:
        return list(
            (
                await db.execute(
                    select(ModelCallRow)
                    .where(ModelCallRow.run_id == run_id)
                    .order_by(ModelCallRow.created_at, ModelCallRow.retry_index)
                )
            )
            .scalars()
            .all()
        )


async def test_truncated_response_recovers_and_tool_dispatches_exactly_once(database, unique_name):
    context = await context_for(database, unique_name)
    executed: list[str] = []

    @tool
    def local_probe(value: str) -> str:
        """记录一次本地探测。"""
        executed.append(value)
        return f"echo:{value}"

    model = ScriptedChatModel(
        responses=[
            _truncated(),
            tool_call("local_probe", {"value": "once"}, "call-once"),
            final_text("恢复后得到结论"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[local_probe],
        system_prompt="基础系统提示",
        state_schema=AgentGraphState,
        context_schema=RunContext,
        middleware=_middlewares(context),
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="检查恢复", id="u1")]}), context=context
    )

    assert executed == ["once"], "坏响应不得派发任何工具；恢复后的工具只能执行一次"
    assert result["messages"][-1].content == "恢复后得到结论"
    calls = await _calls(database, context.toolbox.run_id)
    # 第一个逻辑决策：初次请求被截断 -> 一次恢复 -> 可用（两次网络尝试共用 logical id）；
    # 工具执行后的下一个决策是新的 logical id。
    first_decision = [call for call in calls if call.logical_call_id == calls[0].logical_call_id]
    assert [call.retry_index for call in first_decision] == [0, 1]
    assert len(first_decision) == 2, "同一次逻辑决策两次尝试，各自独立入账"
    assert len({call.model_call_id for call in calls}) == len(calls)
    assert len({call.logical_call_id for call in calls}) == 2
    assert all(call.phase == "agent" for call in calls)
    async with database.session() as db:
        events = list(
            (
                await db.execute(
                    select(EventRow)
                    .where(
                        EventRow.run_id == context.toolbox.run_id,
                        EventRow.type == "model.response_quality",
                    )
                    .order_by(EventRow.session_seq)
                )
            )
            .scalars()
            .all()
        )
    first_events = [
        event
        for event in events
        if event.payload["logical_call_id"] == str(first_decision[0].logical_call_id)
    ]
    assert [event.payload["outcome"] for event in first_events] == ["truncated", "usable"]
    assert [event.payload["attempt_index"] for event in first_events] == [0, 1]


async def test_recovery_exhaustion_fails_without_dispatching_any_tool(database, unique_name):
    context = await context_for(database, unique_name)
    executed: list[str] = []

    @tool
    def local_probe(value: str) -> str:
        """记录一次本地探测。"""
        executed.append(value)
        return value

    model = ScriptedChatModel(responses=[_empty()])
    graph = create_agent(
        model=model,
        tools=[local_probe],
        state_schema=AgentGraphState,
        context_schema=RunContext,
        middleware=_middlewares(context, max_recoveries=2),
    )
    with pytest.raises(ModelResponseUnavailable):
        await graph.ainvoke(
            cast(Any, {"messages": [HumanMessage(content="永远得不到结论", id="u1")]}),
            context=context,
        )
    assert executed == []
    calls = await _calls(database, context.toolbox.run_id)
    assert len(calls) == 3, "初次请求 + 两次自动恢复"
    assert [call.retry_index for call in calls] == [0, 1, 2]


async def test_refusal_is_not_retried_to_bypass_it(database, unique_name):
    context = await context_for(database, unique_name)
    model = ScriptedChatModel(responses=[_refused()])
    graph = create_agent(
        model=model,
        tools=[],
        state_schema=AgentGraphState,
        context_schema=RunContext,
        middleware=_middlewares(context),
    )
    with pytest.raises(ModelResponseRefused):
        await graph.ainvoke(
            cast(Any, {"messages": [HumanMessage(content="被拒绝的请求", id="u1")]}),
            context=context,
        )
    calls = await _calls(database, context.toolbox.run_id)
    assert len(calls) == 1, "明确拒绝不得自动重试"


async def test_normal_chat_still_returns_plain_text(database, unique_name):
    context = await context_for(database, unique_name)
    model = ScriptedChatModel(responses=[final_text("普通聊天回复")])
    graph = create_agent(
        model=model,
        tools=[],
        state_schema=AgentGraphState,
        context_schema=RunContext,
        middleware=_middlewares(context),
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="你好", id="u1")]}), context=context
    )
    assert result["messages"][-1].content == "普通聊天回复"
    assert len(await _calls(database, context.toolbox.run_id)) == 1


async def _reject_all_chain(
    middleware: ResponseRecoveryMiddleware, request: ModelRequest[Any]
) -> None:
    """按真实嵌套调用：ResponseRecovery -> ContextBuilder -> ModelObserver -> 模型。"""
    observer = ModelObserverMiddleware()
    builder = ContextBuilderMiddleware(profile())

    async def model_call(_request: Any) -> ModelResponse:
        return ModelResponse(result=[_empty()])

    async def inner(request_inner: Any) -> ModelResponse:
        return await observer.awrap_model_call(request_inner, model_call)

    async def handler(request_outer: Any) -> ModelResponse:
        return await builder.awrap_model_call(request_outer, inner)

    await middleware.awrap_model_call(request, handler)


async def test_restart_does_not_regain_unlimited_recovery_quota(database, unique_name):
    context = await context_for(database, unique_name)
    middleware = ResponseRecoveryMiddleware(max_recoveries=2)

    def request_for() -> ModelRequest[Any]:
        return ModelRequest(
            model=ScriptedChatModel(responses=[final_text("unused")]),
            messages=[HumanMessage(content="同一决策", id="u1")],
            system_message=SystemMessage(content="stable-system"),
            tools=[],
            state=cast(Any, {"segment_index": 0}),
            runtime=cast(Any, SimpleNamespace(context=context)),
        )

    with pytest.raises(ModelResponseUnavailable):
        await _reject_all_chain(middleware, request_for())
    first_batch = await _calls(database, context.toolbox.run_id)
    assert len(first_batch) == 3

    # 自动重启：同样的逻辑决策（run + 片段 + 稳定消息 ID 范围）不得重新获得无限额度。
    for _ in range(3):
        with pytest.raises(ModelResponseUnavailable):
            await _reject_all_chain(middleware, request_for())
    second_batch = await _calls(database, context.toolbox.run_id)
    assert len(second_batch) == 3, "额度耗尽后反复重启也不得新增网络请求"
    assert second_batch[-1].retry_index == 2
    assert len({call.logical_call_id for call in second_batch}) == 1


async def test_pause_during_recovery_gap_stops_before_next_attempt(database, unique_name):
    context = await context_for(database, unique_name)
    checks: list[int] = []

    async def control_check(*_args: Any, **_kwargs: Any) -> None:
        checks.append(1)
        # 第 1 次来自恢复循环、第 2 次来自本次网络尝试的入口；第 3 次即"恢复间隙"。
        if len(checks) >= 3:
            raise GraphRunAborted("paused", detail="用户在恢复间隙请求暂停")

    context.toolbox.assert_control_current = control_check  # type: ignore[attr-defined]
    middleware = ResponseRecoveryMiddleware(max_recoveries=2)

    request: ModelRequest[Any] = ModelRequest(
        model=ScriptedChatModel(responses=[final_text("unused")]),
        messages=[HumanMessage(content="暂停测试", id="u1")],
        system_message=SystemMessage(content="stable-system"),
        tools=[],
        state=cast(Any, {"segment_index": 0}),
        runtime=cast(Any, SimpleNamespace(context=context)),
    )
    with pytest.raises(GraphRunAborted):
        await _reject_all_chain(middleware, request)
    calls = await _calls(database, context.toolbox.run_id)
    assert len(calls) == 1, "已失效的响应不再被接受，也不再发起下一次尝试"


async def test_mixed_valid_and_broken_tool_batch_dispatches_nothing(database, unique_name):
    context = await context_for(database, unique_name)
    executed: list[str] = []

    @tool
    def local_probe(value: str) -> str:
        """记录一次本地探测。"""
        executed.append(value)
        return value

    broken = AIMessage.model_construct(
        content="",
        tool_calls=[
            {"name": "local_probe", "args": {"value": "ok"}, "id": "c1", "type": "tool_call"},
            {"name": "local_probe", "args": "unfinished-json", "id": "c2", "type": "tool_call"},
        ],
    )
    model = ScriptedChatModel(responses=[broken, final_text("改为直接结论")])
    graph = create_agent(
        model=model,
        tools=[local_probe],
        state_schema=AgentGraphState,
        context_schema=RunContext,
        middleware=_middlewares(context),
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="混合批次", id="u1")]}), context=context
    )
    assert executed == [], "同批含有破损调用时整批不派发"
    assert result["messages"][-1].content == "改为直接结论"
    async with database.session() as db:
        events = list(
            (
                await db.execute(
                    select(EventRow).where(
                        EventRow.run_id == context.toolbox.run_id,
                        EventRow.type == "model.response_quality",
                    )
                )
            )
            .scalars()
            .all()
        )
    outcomes = [event.payload["outcome"] for event in events]
    assert outcomes[0] == "invalid_tool_call" and outcomes[-1] == "usable"
    # 破损批次不写入可执行历史：最终消息里没有对应 ToolMessage。
    assert not [item for item in result["messages"] if isinstance(item, ToolMessage)]


def test_recovery_feedback_is_short_and_observable() -> None:
    from agent_workbench.models.response_quality import classify_response, recovery_feedback

    quality = classify_response(ModelResponse(result=[_truncated()]))
    message = recovery_feedback(quality)
    assert "长度限制" in message and len(message) < 200
    assert json.dumps(message, ensure_ascii=False)
