"""只在精确消息 ID 匹配时记录首个可见模型分片。"""

from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, AIMessageChunk

from agent_workbench.observability.model_stream import ModelStreamTracker


def test_graph_review_text_does_not_become_user_report_after_request_returns():
    tracker = ModelStreamTracker()
    note = AIMessageChunk(content="维护说明", id="review")
    final = AIMessageChunk(content="最终报告", id="final")
    with tracker.track_call("review-call", lambda: None, purpose="graph_review"):
        tracker.observe((note, {"langgraph_node": "model"}))
        assert tracker.is_auxiliary(note)
    assert tracker.is_auxiliary(note)  # delayed stream delivery keeps its purpose
    assert not tracker.is_auxiliary(final)
    with tracker.track_call("normal-call", lambda: None):
        tracker.observe((final, {"langgraph_node": "model"}))
        assert not tracker.is_auxiliary(final)


def test_unknown_and_unrelated_chunks_do_not_create_first_token() -> None:
    tracker = ModelStreamTracker()
    now = datetime.now(UTC)
    tracker.observe(
        (AIMessageChunk(content="内容", id="other"), {"langgraph_node": "model"}), observed_at=now
    )
    tracker.observe(
        (AIMessageChunk(content="内容", id="wanted"), {"langgraph_node": "tools"}), observed_at=now
    )
    tracker.observe(
        (AIMessageChunk(content="", id="wanted"), {"langgraph_node": "model"}), observed_at=now
    )
    assert (
        tracker.take_for_response(
            [AIMessage(content="正文", id="wanted")],
            submitted_at=now - timedelta(seconds=1),
            completed_at=now + timedelta(seconds=1),
        )
        is None
    )


def test_tool_call_chunk_counts_only_when_id_and_time_match() -> None:
    tracker = ModelStreamTracker()
    now = datetime.now(UTC)
    chunk = AIMessageChunk(
        content="",
        id="call-1",
        tool_call_chunks=[{"name": "http_request", "args": "{}", "id": "tool-1", "index": 0}],
    )
    tracker.observe((chunk, {"langgraph_node": "model"}), observed_at=now)
    tracker.observe(
        (AIMessageChunk(content="后续", id="call-1"), {"langgraph_node": "model"}),
        observed_at=now + timedelta(milliseconds=5),
    )
    assert (
        tracker.take_for_response(
            [AIMessage(content="", id="call-1")],
            submitted_at=now - timedelta(seconds=1),
            completed_at=now + timedelta(seconds=1),
        )
        == now
    )
    assert (
        tracker.take_for_response(
            [AIMessage(content="", id="call-1")],
            submitted_at=now - timedelta(seconds=1),
            completed_at=now + timedelta(seconds=1),
        )
        is None
    )
    tracker.observe((chunk, {"langgraph_node": "model"}), observed_at=now)
    assert (
        tracker.take_for_response(
            [AIMessage(content="", id="call-1")],
            submitted_at=now + timedelta(seconds=1),
            completed_at=now + timedelta(seconds=2),
        )
        is None
    )


def test_reasoning_is_activity_without_becoming_visible_text_or_first_token():
    tracker = ModelStreamTracker()
    activity = []
    now = datetime.now(UTC)
    with tracker.track_call("physical-call", lambda: activity.append("tick")):
        reasoning = AIMessageChunk(
            content="", id="thinking", additional_kwargs={"reasoning_content": "fixture"}
        )
        tracker.observe((reasoning, {"langgraph_node": "tools"}))
        tracker.observe((AIMessageChunk(content="", id="empty"), {"langgraph_node": "model"}))
        assert activity == []
        tracker.observe((reasoning, {"langgraph_node": "model"}), observed_at=now)
        assert tracker.activity_phase == "thinking"
        assert activity == ["tick"]
        assert (
            tracker.take_for_response(
                [AIMessage(content="", id="thinking")], submitted_at=now, completed_at=now
            )
            is None
        )
    assert tracker.active_call_id is None
    tracker.observe((reasoning, {"langgraph_node": "model"}))
    assert activity == ["tick"]
