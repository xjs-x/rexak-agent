"""Run 状态机、工具参数容错、工件脱敏、操作身份摘要、资源并发。

这些是不需要数据库的纯逻辑测试，属于"静态与契约"层（ACCEPTANCE §1.1）。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.contracts.enums import ErrorCode, RunStatus, ToolEffectClass
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.run_state import (
    ALLOWED_TRANSITIONS,
    assert_transition,
    can_transition,
)
from agent_workbench.evidence.artifacts import redact_text
from agent_workbench.persistence.repositories import compute_request_digest
from agent_workbench.tools.params import coerce_json_object, coerce_str_list
from agent_workbench.tools.runtime import ResourceBusyTimeout, ResourceLocks

# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for status in (RunStatus.COMPLETED, RunStatus.CANCELLED):
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_completed_run_cannot_be_resumed() -> None:
    """completed 的 run 必须走"新 run"，不能 resume（§4.2）。"""
    assert not can_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
    with pytest.raises(ApiError) as excinfo:
        assert_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
    assert excinfo.value.code is ErrorCode.CONFLICT


def test_pausing_must_pass_through_paused() -> None:
    assert can_transition(RunStatus.PAUSING, RunStatus.PAUSED)
    assert not can_transition(RunStatus.PAUSING, RunStatus.RUNNING)


def test_failed_run_can_only_go_to_recovering() -> None:
    assert ALLOWED_TRANSITIONS[RunStatus.FAILED] == frozenset({RunStatus.RECOVERING})


def test_waiting_tool_can_resume_to_running() -> None:
    assert can_transition(RunStatus.WAITING_TOOL, RunStatus.RUNNING)


# --------------------------------------------------------------------------- #
# 工具参数容错
# --------------------------------------------------------------------------- #


def test_coerce_list_accepts_three_equivalent_forms() -> None:
    assert coerce_str_list(None, "f") == []
    assert coerce_str_list(["a", "b"], "f") == ["a", "b"]
    assert coerce_str_list('["a","b"]', "f") == ["a", "b"]
    assert coerce_str_list("a", "f") == ["a"]


def test_coerce_list_rejects_object_with_clear_error() -> None:
    """模型给对象时必须给**确定错误**，不能猜测含义。"""
    with pytest.raises(ValueError, match="不是数组"):
        coerce_str_list('{"a": "b"}', "artifact_ids")


def test_coerce_object_roundtrip() -> None:
    assert coerce_json_object(None, "f") == {}
    assert coerce_json_object({"a": 1}, "f") == {"a": 1}
    assert coerce_json_object('{"a": 1}', "f") == {"a": 1}
    with pytest.raises(ValueError):
        coerce_json_object("[1,2]", "f")


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #


def test_redact_masks_values_but_keeps_field_names() -> None:
    text = "username=alice&password=hunter2&token=abc123def456"
    masked = redact_text(text)
    assert "hunter2" not in masked
    assert "abc123def456" not in masked
    assert "password" in masked, "保留字段名便于理解结构"


def test_redact_masks_bearer_tokens() -> None:
    masked = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in masked


def test_redact_strips_terminal_control_characters() -> None:
    """工具结果里的控制字符必须被剥离，避免污染日志与终端。"""
    masked = redact_text("normal\x1b[31mred\x07text")
    assert "\x1b" not in masked
    assert "\x07" not in masked
    assert "normal" in masked and "text" in masked


def test_redact_keeps_newlines_and_tabs() -> None:
    masked = redact_text("a\nb\tc")
    assert masked == "a\nb\tc"


# --------------------------------------------------------------------------- #
# 操作身份摘要
# --------------------------------------------------------------------------- #


def test_request_digest_is_order_insensitive_for_dicts() -> None:
    assert compute_request_digest({"a": 1, "b": 2}) == compute_request_digest({"b": 2, "a": 1})


def test_request_digest_changes_with_values() -> None:
    assert compute_request_digest({"url": "/a"}) != compute_request_digest({"url": "/b"})


# --------------------------------------------------------------------------- #
# 资源并发
# --------------------------------------------------------------------------- #


async def _locks_serialize() -> None:
    from agent_workbench.config.settings import get_settings

    settings = get_settings().model_copy(update={"tool_max_concurrent": 8})
    locks = ResourceLocks(settings)
    events: list[str] = []

    async def worker(tag: str) -> None:
        async with locks.acquire(["file:shared"], timeout=2):
            events.append(f"{tag}-in")
            await asyncio.sleep(0.05)
            events.append(f"{tag}-out")

    await asyncio.gather(worker("a"), worker("b"))
    # 共享副作用必须串行：不允许出现 a-in,b-in 这种交错
    assert events in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


async def _locks_timeout_reports_blocked() -> None:
    from agent_workbench.config.settings import get_settings

    locks = ResourceLocks(get_settings())

    async def hold() -> None:
        async with locks.acquire(["file:x"], timeout=5):
            await asyncio.sleep(0.3)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.05)
    with pytest.raises(ResourceBusyTimeout):
        async with locks.acquire(["file:x"], timeout=0.05):
            pass
    await holder


def test_file_lock_serializes_shared_state() -> None:
    asyncio.run(_locks_serialize())


def test_lock_timeout_is_reported_as_blocked_not_failure() -> None:
    asyncio.run(_locks_timeout_reports_blocked())


# --------------------------------------------------------------------------- #
# 工具效果类别（决定是否可自动重试）
# --------------------------------------------------------------------------- #


def test_effect_classes_are_explicit() -> None:
    assert {item.value for item in ToolEffectClass} == {
        "read_only",
        "idempotent_write",
        "side_effecting",
    }
