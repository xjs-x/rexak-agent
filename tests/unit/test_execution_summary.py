"""事件聚合摘要与模板参数替换（验收 F08/F11、B07 的确定性部分）。

- 摘要由确定性规则生成：资源用途、事件类型、首次/最后时间、次数、重要结果、缺失；
- 摘要文本显式声明"事件通知不代表程序执行成功或任务目标达成"；
- 模板参数按 argv 元素占位替换（不做 shell 拼接），参数校验只承诺子集。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_workbench.runtime.execution_events import (
    render_summary_text,
    summarize_events,
)
from agent_workbench.tools.execution_resources import _substitute_argv, _validate_params


def _receipt(event_type: str, *, resource_id: str = "res-1", payload=None, minutes: int = 0):
    return SimpleNamespace(
        id=1,
        type=event_type,
        resource_id=resource_id,
        payload=payload or {},
        occurred_at=datetime(2026, 10, 15, 12, minutes, tzinfo=UTC),
        availability="available",
    )


def test_summary_is_deterministic_and_bounded():
    receipts = [
        _receipt("resource.exited", payload={"exit_code": 0, "termination_reason": "exited_normally"}, minutes=1),
        _receipt("resource.watch_match", payload={"pattern": "login ok"}, minutes=2),
        _receipt("output.limit_reached", payload={"action": "preserve_process", "dropped_bytes": 1024}, minutes=3),
    ]
    summary = summarize_events(receipts)
    assert summary["event_count"] == 3
    assert summary["by_type"] == {
        "output.limit_reached": 1,
        "resource.exited": 1,
        "resource.watch_match": 1,
    }
    assert summary["first_occurred_at"] < summary["last_occurred_at"]
    assert any("退出" in item for item in summary["important"])
    assert any("监视命中" in item for item in summary["important"])
    assert any("日志超限" in item for item in summary["important"])
    # 确定性：同输入两次生成一致
    assert summarize_events(receipts) == summary


def test_summary_preserves_pending_semantics_note():
    receipts = [_receipt("resource.warning", payload={"warning": "x"}, minutes=i) for i in range(3)]
    text = render_summary_text(summarize_events(receipts))
    assert "系统通知·执行事件聚合" in text
    assert "不代表程序执行成功或任务目标达成" in text
    assert "事件 3 条" in text


def test_summary_marks_availability_notes():
    receipt = _receipt("resource.exited", payload={"exit_code": None})
    receipt.availability = "truncated"
    summary = summarize_events([receipt])
    assert summary["availability_notes"]
    text = render_summary_text(summary)
    assert "证据可用性" in text


def test_summary_reports_delegation_mode():
    delegation = cast(Any, SimpleNamespace(id=uuid4(), mode="continuous", goal="盯住登录成功"))
    text = render_summary_text(summarize_events([_receipt("resource.exited")], delegation=delegation), delegation=delegation)
    assert "持续值班" in text
    assert "盯住登录成功" in text


def test_argv_substitution_is_data_not_code():
    argv = _substitute_argv(
        ["/usr/bin/tool", "--target", "{host}", "--note", "{note}"],
        {"host": "example.test; rm -rf /", "note": "$(whoami)"},
    )
    assert argv == [
        "/usr/bin/tool",
        "--target",
        "example.test; rm -rf /",
        "--note",
        "$(whoami)",
    ]
    with pytest.raises(ValueError, match="缺少"):
        _substitute_argv(["{missing}"], {})


def test_param_validation_subset_is_honest():
    schema = {
        "type": "object",
        "required": ["host"],
        "properties": {
            "host": {"type": "string"},
            "port": {"type": "integer"},
            "mode": {"type": "string", "enum": ["fast", "deep"]},
        },
    }
    assert _validate_params(schema, {"host": "a", "port": 80, "mode": "fast"}) == []
    problems = _validate_params(schema, {"port": "80", "mode": "slow"})
    assert any("host" in item for item in problems)
    assert any("port" in item for item in problems)
    assert any("mode" in item for item in problems)
