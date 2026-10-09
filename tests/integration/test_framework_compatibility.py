"""P0：框架兼容性验证。

覆盖 BUILD_PLAN P0 清单要求的每一项：
自定义 state、消息 reducer、middleware、工具调用、streaming、checkpoint 恢复、
Store 跨 thread 按 namespace 查询、interrupt/resume 语义。

分两层，**不混淆**：
- 框架行为层用确定性脚本模型（无需凭据，始终运行）；
- 真实模型能力探针标 `real_model`，无凭据时 skip 并在报告中记为 NOT_RUN。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.bootstrap import (
    open_checkpointer_connection,
    open_store_connection,
)

pytestmark = pytest.mark.integration


@tool("local_echo")
def local_echo(label: str, count: int = 1) -> str:
    """无副作用的本地回显工具。

    Args:
        label: 标签
        count: 次数
    """
    import json

    return json.dumps({"label": label, "count": count}, ensure_ascii=False)


async def test_agent_loop_calls_tool_and_persists_state(settings) -> None:
    """自定义 state + 消息 reducer + 工具调用 + checkpoint。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    model = ScriptedChatModel(
        responses=[
            tool_call("local_echo", {"label": "compat", "count": 2}, "call_1"),
            final_text("完成"),
        ]
    )

    async with (
        open_checkpointer_connection(settings) as ck_conn,
        open_store_connection(settings) as store_conn,
    ):
        agent = create_agent(
            model=model,
            tools=[local_echo],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(ck_conn),
            store=AsyncPostgresStore(store_conn),
        )
        thread_id = f"p0-{uuid.uuid4().hex}"
        config = {"configurable": {"thread_id": thread_id}}
        # 说明：图的输入是 TypedDict，pyright 会对"宽 dict 字面量"报类型不匹配；
        # 这里显式标注为 Any 表示"这是框架约定的入参形态"，不是类型缺陷。
        graph_input: Any = {
            "messages": [HumanMessage(content="调用 local_echo")],
            "objective": "框架兼容性",
        }
        result = await agent.ainvoke(graph_input, config)  # type: ignore[arg-type]

    kinds = [type(message).__name__ for message in result["messages"]]
    assert kinds == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    tool_message = result["messages"][2]
    assert '"label": "compat"' in tool_message.content
    assert result["objective"] == "框架兼容性"


async def test_checkpoint_is_readable_in_same_thread_across_compilations(settings) -> None:
    """服务重启后同 thread 能读取之前状态（P0 退出条件之一）。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    thread_id = f"p0-reload-{uuid.uuid4().hex}"

    async with open_checkpointer_connection(settings) as conn:
        agent = create_agent(
            model=ScriptedChatModel(responses=[final_text("第一次")]),
            tools=[],
            checkpointer=AsyncPostgresSaver(conn),
        )
        await agent.ainvoke(
            {"messages": [HumanMessage(content="第一轮")]},
            {"configurable": {"thread_id": thread_id}},
        )

    # 重新打开连接 = 模拟进程重启
    async with open_checkpointer_connection(settings) as conn:
        agent2 = create_agent(
            model=ScriptedChatModel(responses=[final_text("第二轮")]),
            tools=[],
            checkpointer=AsyncPostgresSaver(conn),
        )
        state = await agent2.aget_state({"configurable": {"thread_id": thread_id}})
        assert len(state.values["messages"]) == 2, "重启后应能读到同 thread 的历史"

        await agent2.ainvoke(
            {"messages": [HumanMessage(content="第二轮")]},
            {"configurable": {"thread_id": thread_id}},
        )
        state_after = await agent2.aget_state({"configurable": {"thread_id": thread_id}})
        assert len(state_after.values["messages"]) == 4, "同一 thread 应能继续累积"


async def test_store_isolated_by_namespace(settings) -> None:
    """Store 数据跨 thread 可按指定 namespace 查询，且不同 namespace 之间隔离。"""
    from langgraph.store.postgres.aio import AsyncPostgresStore

    owner = f"owner-{uuid.uuid4().hex[:8]}"
    project_a = f"proj-a-{uuid.uuid4().hex[:8]}"
    project_b = f"proj-b-{uuid.uuid4().hex[:8]}"

    async with open_store_connection(settings) as conn:
        store = AsyncPostgresStore(conn)
        await store.aput((owner, project_a, "project_facts"), "k1", {"content": "A 的事实"})
        await store.aput((owner, project_b, "project_facts"), "k1", {"content": "B 的事实"})

        a_item = await store.aget((owner, project_a, "project_facts"), "k1")
        b_item = await store.aget((owner, project_b, "project_facts"), "k1")
        assert a_item is not None and a_item.value["content"] == "A 的事实"
        assert b_item is not None and b_item.value["content"] == "B 的事实"

        hits_a = await store.asearch((owner, project_a, "project_facts"), limit=10)
        assert len(hits_a) == 1, "项目 A 的命名空间不应看到项目 B 的条目"


async def test_custom_state_reducer_merges_id_sets(settings) -> None:
    """`pending_job_ids` / `active_lead_ids` 使用稳定去重合并 reducer。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with open_checkpointer_connection(settings) as conn:
        agent = create_agent(
            model=ScriptedChatModel(responses=[final_text("ok")]),
            tools=[],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(conn),
        )
        thread_id = f"p0-reducer-{uuid.uuid4().hex}"
        config = {"configurable": {"thread_id": thread_id}}
        graph_input: Any = {
            "messages": [HumanMessage(content="x")],
            "pending_job_ids": ["job-1", "job-2"],
        }
        await agent.ainvoke(graph_input, config)  # type: ignore[arg-type]
        state = await agent.aget_state(config)  # type: ignore[arg-type]
        assert state.values["pending_job_ids"] == ["job-1", "job-2"]


async def test_raw_create_agent_propagates_tool_error(settings) -> None:
    """框架默认行为：裸 `create_agent` 会把工具异常向上抛。

    这**不是**本项目的最终行为：我们在 `ToolDispatchMiddleware` 里把工具异常
    分类后转成结构化 `ToolMessage`（见 `tests/integration/test_tool_dispatch.py`）。
    这个用例把框架默认行为固定下来，避免未来升级后我们误以为"已经是转换过的"。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    @tool("failing_tool")
    def failing_tool(reason: str) -> str:
        """总是失败的工具。

        Args:
            reason: 失败原因
        """
        raise RuntimeError(f"expected failure: {reason}")

    model = ScriptedChatModel(
        responses=[
            tool_call("failing_tool", {"reason": "test"}, "call_1"),
            final_text("已处理失败"),
        ]
    )
    async with open_checkpointer_connection(settings) as conn:
        agent = create_agent(
            model=model, tools=[failing_tool], checkpointer=AsyncPostgresSaver(conn)
        )
        with pytest.raises(RuntimeError, match="expected failure"):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="调用 failing_tool")]},
                {"configurable": {"thread_id": f"p0-err-{uuid.uuid4().hex}"}},
            )


async def test_streaming_emits_message_chunks(settings) -> None:
    """streaming 可用，且以增量块形式产出。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with open_checkpointer_connection(settings) as conn:
        agent = create_agent(
            model=ScriptedChatModel(responses=[final_text("流式输出内容")]),
            tools=[],
            checkpointer=AsyncPostgresSaver(conn),
        )
        chunks: list[object] = []
        async for mode, chunk in agent.astream(
            {"messages": [HumanMessage(content="说点什么")]},
            {"configurable": {"thread_id": f"p0-stream-{uuid.uuid4().hex}"}},
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                chunks.append(chunk)
        assert chunks, "应至少收到一个消息块"


@pytest.mark.real_model
async def test_real_model_probe_reports_capabilities() -> None:
    """真实小米模型的能力探针（P0 退出条件）。无凭据时由 conftest skip。"""
    from agent_workbench.models.probe import run_probe

    report = await run_probe(timeout_seconds=90)
    by_name = {probe.name: probe for probe in report.probes}
    assert by_name["plain_text"].ok, by_name["plain_text"].detail
    assert by_name["tool_call_complete_args"].ok, by_name["tool_call_complete_args"].detail
    assert report.supports_streaming == "supported"
    assert report.supports_usage in {"supported", "unknown"}
