"""响应完整性分类：空结构、空正文、截断、拒绝、无效工具调用与正常内容块。"""

from __future__ import annotations

from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from agent_workbench.models.response_quality import (
    batch_rejection,
    classify_response,
    recovery_feedback,
    visible_text,
)


def _response(message: AIMessage) -> ModelResponse:
    return ModelResponse(result=[message])


def test_empty_structures_and_empty_text_are_classified_empty() -> None:
    assert classify_response(ModelResponse(result=[])).outcome == "empty"
    assert classify_response(_response(AIMessage(content=""))).outcome == "empty"
    # 空结构块不能被 str(content) 误算成有效正文。
    structured = AIMessage(content=[{"type": "tool_use", "id": "x"}])
    result = classify_response(_response(structured))
    assert result.outcome == "empty" and result.visible_chars == 0


def test_length_truncation_wins_over_looking_complete_tools() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": "http_request", "args": {"url": "x"}, "id": "c1", "type": "tool_call"}],
        response_metadata={"finish_reason": "length"},
    )
    quality = classify_response(_response(message))
    assert quality.outcome == "truncated"
    assert quality.valid_tool_count == 1
    assert "长度限制" in recovery_feedback(quality)


def test_refusal_is_not_retried_and_visible_reason_is_preserved() -> None:
    by_metadata = AIMessage(content="无法协助", response_metadata={"finish_reason": "content_filter"})
    assert classify_response(_response(by_metadata)).outcome == "refused"
    by_kwargs = AIMessage(content="无法协助", additional_kwargs={"refusal": "policy"})
    assert classify_response(_response(by_kwargs)).outcome == "refused"


def test_invalid_tool_calls_are_separated_from_valid_ones() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": "http_request", "args": {}, "id": "c1", "type": "tool_call"}],
        invalid_tool_calls=[{"name": "http_request", "args": "{not json", "id": "c2"}],
    )
    quality = classify_response(_response(message))
    assert quality.outcome == "invalid_tool_call"
    assert quality.valid_tool_count == 1 and quality.invalid_tool_count == 1


def test_missing_finish_reason_with_text_or_tools_is_usable() -> None:
    assert classify_response(_response(AIMessage(content="一段结论"))).outcome == "usable"
    with_tool = AIMessage(
        content="",
        tool_calls=[{"name": "record_lead", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    assert classify_response(_response(with_tool)).outcome == "usable"


def test_multimodal_response_counts_only_text_blocks() -> None:
    message = AIMessage(
        content=[
            {"type": "text", "text": "可见正文"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    )
    assert visible_text(message) == "可见正文"
    quality = classify_response(_response(message))
    assert quality.outcome == "usable" and quality.visible_chars == 4


def test_blank_messages_without_ai_message_are_empty() -> None:
    assert classify_response(None).outcome == "empty"
    assert classify_response(ModelResponse(result=[ToolMessage(content="x", tool_call_id="t")]))\
        .outcome == "empty"


def test_batch_rejection_flags_duplicate_plan_updates_and_broken_calls() -> None:
    duplicated = AIMessage(
        content="",
        tool_calls=[
            {"name": "update_plan", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "update_plan", "args": {}, "id": "c2", "type": "tool_call"},
        ],
    )
    assert "只保留一个" in (batch_rejection(_response(duplicated)) or "")
    no_id = AIMessage(
        content="",
        tool_calls=[{"name": "http_request", "args": {}, "id": "", "type": "tool_call"}],
    )
    assert "缺少稳定 id" in (batch_rejection(_response(no_id)) or "")
    # AIMessage 的校验会拒绝非 dict 的 args，因此这里绕过校验模拟"分片未拼完"的入参。
    bad_args = AIMessage.model_construct(
        content="",
        tool_calls=[{"name": "http_request", "args": "raw", "id": "c1", "type": "tool_call"}],
    )
    assert "不是 JSON 对象" in (batch_rejection(_response(bad_args)) or "")
    healthy = AIMessage(
        content="",
        tool_calls=[{"name": "update_plan", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    assert batch_rejection(_response(healthy)) is None


def test_recovery_feedback_never_asks_for_hidden_reasoning() -> None:
    for outcome in ("truncated", "invalid_tool_call", "empty"):
        message = AIMessage(content="")
        quality = classify_response(_response(message))
        text = recovery_feedback(
            type(quality)(outcome, None, 0, 0, 0)  # type: ignore[arg-type]
        )
        assert text.strip()
        assert "推理" not in text


def test_coherence_rejects_repeated_prefix_and_recap():
    from agent_workbench.models.response_quality import coherence_rejection

    previous = "当前入口返回 403，说明需要认证。" + "这是上一轮的说明。" * 8
    repeated = previous[:120] + "本轮继续补充。"
    assert "重复" in (coherence_rejection(previous, repeated) or "")
    recap = "总结一下：" + previous
    assert "recap" in (coherence_rejection(previous, recap) or "")


def test_coherence_rejects_self_proof_without_evidence_and_allows_evidence():
    from agent_workbench.models.response_quality import coherence_rejection

    self_proof = "我已经验证了目标存在漏洞，测试通过。" + "没有更多信息。" * 12
    assert coherence_rejection("", self_proof) is not None
    with_evidence = self_proof + " 证据工件 artifact-123。"
    assert coherence_rejection("", with_evidence) is None


def test_coherence_skips_tool_actions_and_long_text_is_rejected():
    from agent_workbench.models.response_quality import coherence_rejection

    assert coherence_rejection("旧内容" * 50, "新内容" * 50, has_tool_calls=True) is None
    long_text = "事实。" * 2100
    assert "过长" in (coherence_rejection("", long_text) or "")


def test_previous_visible_text_uses_last_ai_message():
    from agent_workbench.models.response_quality import previous_visible_text

    messages = [
        AIMessage(content="第一轮"),
        ToolMessage(content="工具输出", tool_call_id="t"),
        AIMessage(content="第二轮"),
    ]
    assert previous_visible_text(messages) == "第二轮"
