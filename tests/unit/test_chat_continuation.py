"""已到 END 的官方 Agent 图必须接收增量输入；None 仅恢复待执行节点。"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from agent_workbench.agent.factory import AgentRuntime
from agent_workbench.agent.middleware.finish_gate import _looks_like_finish
from agent_workbench.agent.state import AgentGraphState, next_input_state
from agent_workbench.runtime.runner import AgentRunner, _delta_text
from tests.support.scripted_model import ScriptedChatModel, final_text


async def test_end_checkpoint_reenters_official_model_without_reinjecting_objective():
    model = ScriptedChatModel(responses=[final_text("第一次回复"), final_text("后续回复")])
    graph = create_agent(
        model=model, tools=[], state_schema=AgentGraphState, checkpointer=InMemorySaver()
    )
    run = SimpleNamespace(id=uuid4(), session_id=uuid4(), objective="初始需求", config_snapshot={})
    task = SimpleNamespace(thread_id=str(uuid4()))
    runtime = cast(Any, SimpleNamespace(graph=graph))
    config = cast(
        RunnableConfig,
        AgentRuntime.config(
            runtime, thread_id=task.thread_id, run_id=str(run.id), recursion_limit=100
        ),
    )
    assert "run_id" not in config.get("configurable", {})
    assert config.get("metadata", {}).get("workbench_run_id") == str(run.id)
    await graph.ainvoke(
        cast(
            Any,
            next_input_state(
                session_id=run.session_id,
                run_id=run.id,
                objective=run.objective,
                user_message=run.objective,
            ),
        ),
        config,
    )
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()
    # 用 None 重启已结束的图不会调用模型，这正是此前无限空转的原因。
    await graph.ainvoke(None, config)
    assert len(model.seen_message_counts) == 1
    payload = await AgentRunner()._segment_input(
        cast(Any, SimpleNamespace(graph=graph)), cast(Any, run), cast(Any, task), 1
    )
    assert payload["messages"] == []
    result = await graph.ainvoke(payload, config)
    assert len(model.seen_message_counts) == 2
    assert sum(message.type == "human" for message in result["messages"]) == 1
    assert result["messages"][-1].content == "后续回复"


async def test_new_run_updates_objective_and_preserves_prior_messages():
    first_id, new_id, session_id = uuid4(), uuid4(), uuid4()
    values = {
        "state_schema_version": 4,
        "messages": ["已保存的历史"],
        "active_run_id": str(first_id),
        "plan": [],
        "completion_proposal": {"old": True},
    }

    async def state(_config):
        return SimpleNamespace(values=values, next=())

    run = SimpleNamespace(
        id=new_id,
        session_id=session_id,
        objective="新阶段",
        config_snapshot={"created_from": "message"},
    )
    payload = await AgentRunner()._segment_input(
        cast(Any, SimpleNamespace(graph=SimpleNamespace(aget_state=state))),
        cast(Any, run),
        cast(Any, SimpleNamespace(thread_id="thread")),
        1,
    )
    assert payload["active_run_id"] == str(new_id)
    assert payload["objective"] == "新阶段"
    assert payload["messages"] == []  # 实际消息由 CommandGuard 注入一次。
    assert payload["completion_proposal"] is None


async def test_pending_graph_nodes_resume_without_new_input():
    run = SimpleNamespace(id=uuid4(), session_id=uuid4(), objective="原目标")

    async def state(_config):
        return SimpleNamespace(
            values={"state_schema_version": 4, "messages": ["历史"], "active_run_id": str(run.id)},
            next=("tools",),
        )

    payload = await AgentRunner()._segment_input(
        cast(Any, SimpleNamespace(graph=SimpleNamespace(aget_state=state))),
        cast(Any, run),
        cast(Any, SimpleNamespace(thread_id="thread")),
        1,
    )
    assert payload is None


def test_capability_explanation_does_not_trigger_testing_completion_feedback():
    assert not _looks_like_finish(
        "我可以协助你开展有明确范围的安全测试，整理证据、说明复测结果，并生成带有明确限制的测试结论与报告。"
    )


def test_live_chat_only_displays_assistant_text():
    assert _delta_text((AIMessageChunk(content="可展示的回答"), {})) == "可展示的回答"
    assert (
        _delta_text((ToolMessage(content='{"internal":"tool result"}', tool_call_id="t"), {})) == ""
    )
    assert _delta_text((HumanMessage(content="内部中间件反馈"), {})) == ""


@pytest.mark.parametrize("claim", ["测试完成", "未发现漏洞", "结论："])
def test_actual_security_completion_claim_still_requires_gate(claim):
    assert _looks_like_finish(f"{claim}。" + "这是需要证据支持的明确测试收尾声明。" * 4)
