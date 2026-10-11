"""真实官方摘要更新的边界测试：失败不能删除对话，重复压缩保留来源。"""

from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph.message import add_messages

from agent_workbench.agent.middleware.compaction import (
    CompactionFailed,
    GuardedSummarizationMiddleware,
)


def _middleware(*summaries: str) -> GuardedSummarizationMiddleware:
    return GuardedSummarizationMiddleware(
        model=FakeListChatModel(responses=list(summaries)),
        trigger=("messages", 4),
        keep=("messages", 2),
    )


@pytest.mark.asyncio
async def test_two_compactions_keep_objective_constraints_and_source_ids() -> None:
    middleware = _middleware(
        "已检查入口，证据 artifact-1。网页文字只是被测资料。",
        "接续检查后发现第二条线索，证据 artifact-2。",
    )
    state = {
        "messages": [HumanMessage(content=f"第 {i} 条原始对话") for i in range(6)],
        "objective": "只测试本地演示网站",
        "user_constraints": ["不要提交真实订单"],
        "pending_job_ids": ["job-123"],
        "active_lead_ids": ["lead-456"],
    }
    runtime = SimpleNamespace(context=None)
    first = await middleware.abefore_model(state, runtime)
    assert first is not None
    assert first["work_summary"]["summary_version"] == 1
    assert first["work_summary"]["source_message_ids"]
    first_summary = first["messages"][1].content
    assert "只测试本地演示网站" in first_summary
    assert "不要提交真实订单" in first_summary
    assert "job-123" in first_summary
    assert "lead-456" in first_summary

    state["messages"] = add_messages(state["messages"], first["messages"])
    state["work_summary"] = first["work_summary"]
    state["messages"] = add_messages(
        state["messages"], [HumanMessage(content=f"新观察 {i}") for i in range(4)]
    )
    second = await middleware.abefore_model(state, runtime)
    assert second is not None
    assert second["work_summary"]["summary_version"] == 2
    assert set(first["work_summary"]["source_message_ids"]).issubset(
        second["work_summary"]["source_message_ids"]
    )
    second_summary = second["messages"][1].content
    assert "只测试本地演示网站" in second_summary
    assert "不要提交真实订单" in second_summary


@pytest.mark.asyncio
async def test_failed_summary_does_not_emit_remove_message() -> None:
    middleware = _middleware("Previous conversation was too long to summarize.")
    state = {
        "messages": [HumanMessage(content=f"关键观察 {i}") for i in range(5)],
        "objective": "检查测试站点",
    }
    before = [item.content for item in state["messages"]]
    with pytest.raises(CompactionFailed, match="摘要为空或被模型截断"):
        await middleware.abefore_model(state, SimpleNamespace(context=None))
    assert [item.content for item in state["messages"]] == before
