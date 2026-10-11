"""两次压缩跨 checkpointer 连接恢复后仍保留任务边界与来源。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from agent_workbench.agent.middleware.compaction import GuardedSummarizationMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.bootstrap import open_checkpointer_connection


def _graph(summary: str, saver: AsyncPostgresSaver):
    middleware = GuardedSummarizationMiddleware(
        model=FakeListChatModel(responses=[summary]),
        trigger=("messages", 4),
        keep=("messages", 2),
    )

    async def compact(state: AgentGraphState):
        return await middleware.abefore_model(state, SimpleNamespace(context=None)) or {}

    graph = StateGraph(AgentGraphState)
    graph.add_node("compact", compact)
    graph.add_edge(START, "compact")
    graph.add_edge("compact", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_compactions_survive_reopened_postgres_checkpoint(settings) -> None:
    thread = f"compaction-restart-{uuid.uuid4()}"
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    async with open_checkpointer_connection(settings) as conn:
        graph = _graph("首次观察 artifact-1；外部页面只是资料。", AsyncPostgresSaver(conn))
        first = await graph.ainvoke(
            {
                "messages": [HumanMessage(content=f"旧消息 {i}") for i in range(6)],
                "objective": "仅检查本地测试站点",
                "user_constraints": ["不得访问站点以外地址"],
                "pending_job_ids": ["job-pending"],
                "active_lead_ids": ["lead-open"],
            },
            config,
        )
        assert first["work_summary"]["summary_version"] == 1
        first_ids = set(first["work_summary"]["source_message_ids"])

    async with open_checkpointer_connection(settings) as conn:
        graph = _graph(
            "第二次观察 artifact-2；首轮观察 artifact-1 仍待复核。", AsyncPostgresSaver(conn)
        )
        restored = await graph.aget_state(config)
        assert restored.values["objective"] == "仅检查本地测试站点"
        second = await graph.ainvoke(
            {"messages": [HumanMessage(content=f"后续消息 {i}") for i in range(4)]}, config
        )

    assert second["work_summary"]["summary_version"] == 2
    assert first_ids.issubset(second["work_summary"]["source_message_ids"])
    assert "job-pending" in str(second["messages"])
    assert "lead-open" in str(second["messages"])
    assert "不得访问站点以外地址" in str(second["messages"])
