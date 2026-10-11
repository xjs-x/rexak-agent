"""新产生的流式事件不得在分片边界暴露凭据。"""

from __future__ import annotations

from uuid import uuid4

from agent_workbench.api.redaction import redact_event_payload
from agent_workbench.runtime.runner import DELTA_SENSITIVE_MAX_CHARS, _DeltaBuffer


def _buffer() -> _DeltaBuffer:
    return _DeltaBuffer(session_id=uuid4(), run_id=uuid4(), last_flush=0)


def test_split_bearer_and_key_value_are_redacted_before_emission() -> None:
    buffer = _buffer()
    assert buffer.add("请求头 Autho") is None
    assert buffer.add("rization: Bear") is None
    assert buffer.add("er verysecretvalue\n") is None
    assert buffer.add("下一项 token=") is None
    assert buffer.add("anothersecretvalue") is None
    emitted = buffer.take()
    assert emitted is not None
    assert "verysecretvalue" not in emitted
    assert "anothersecretvalue" not in emitted
    assert "<redacted>" in emitted


def test_regular_text_still_streams_with_short_boundary_holdback() -> None:
    text = "正常分析。" * 150
    buffer = _buffer()
    first = buffer.add(text)
    tail = buffer.take()
    assert first is not None and tail is not None
    assert first + tail == text


def test_long_sensitive_value_is_hidden_without_unbounded_buffer() -> None:
    buffer = _buffer()
    assert buffer.add("api_key=" + "x" * DELTA_SENSITIVE_MAX_CHARS) is not None
    assert buffer.hidden_overflow is True
    assert buffer.take() is None


def test_long_key_value_is_redacted_in_full() -> None:
    buffer = _buffer()
    secret = "z" * 600
    assert buffer.add("session_id=" + secret) is None
    emitted = buffer.take()
    assert emitted == "session_id=<redacted>"


def test_legacy_raw_delta_is_hidden_on_browser_read() -> None:
    old = redact_event_payload("message.delta", {"delta": "token=split-secret"})
    assert "split-secret" not in old["delta"]
    assert old["redaction_status"] == "legacy_hidden"
    current = redact_event_payload(
        "message.delta", {"delta": "普通输出", "redaction_version": 1}
    )
    assert current["delta"] == "普通输出"
