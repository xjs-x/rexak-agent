"""分段必须等整批工具结果齐全，并以 checkpoint 游标去重统计。"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from agent_workbench.agent.middleware.segment import SegmentMiddleware


async def test_segment_waits_for_complete_parallel_tool_batch():
    middleware = SegmentMiddleware(1)
    message = AIMessage(
        content="",
        id="a1",
        tool_calls=[
            {"name": "fixture", "args": {}, "id": key, "type": "tool_call"} for key in ("c1", "c2")
        ],
    )
    state = {"messages": [message, ToolMessage(content="done", tool_call_id="c1")]}
    runtime = SimpleNamespace(context=None)
    assert await middleware.abefore_model(state, runtime) is None
    state["messages"].append(ToolMessage(content="done", tool_call_id="c2"))
    update = await middleware.abefore_model(state, runtime)
    assert update is not None and update["jump_to"] == "end"
    assert update["completed_decision_rounds"] == 1
    again = await middleware.abefore_model({**state, **update}, runtime)
    assert again is not None and "completed_decision_rounds" not in again
