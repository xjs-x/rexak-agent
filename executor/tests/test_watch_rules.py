"""watch 规则匹配（验收 F12 的可移植部分）。

覆盖：跨块命中、once/repeat 冷却、非法模式前置拒绝、嵌套量词拒绝、
灾难性回溯的墙钟预算（超时后禁用规则且采集不中断）。
"""

from __future__ import annotations

import time

import pytest
from workbench_executor.watch import WatchMatcher, WatchRuleError, build_rules


def test_literal_match_across_chunks():
    matcher = WatchMatcher(build_rules([{"kind": "literal", "pattern": "login ok"}]))
    update = matcher.feed("... log")
    assert update.matches == []
    update = matcher.feed("in ok ...")
    assert len(update.matches) == 1
    assert update.matches[0].pattern == "login ok"
    # once：命中一次后不再重复通知
    second = matcher.feed("login ok again")
    assert second.matches == []
    matcher.close()


def test_regex_case_insensitive_and_repeat_cooldown():
    matcher = WatchMatcher(
        build_rules(
            [
                {
                    "kind": "regex",
                    "pattern": "token=[0-9a-f]+",
                    "case_sensitive": False,
                    "notify": "repeat",
                    "cooldown_ms": 60_000,
                }
            ]
        )
    )
    first = matcher.feed("TOKEN=ABC123")
    assert len(first.matches) == 1
    # 冷却期内不重复
    assert matcher.feed("token=def456").matches == []
    matcher.close()


def test_invalid_rules_are_rejected_before_running():
    with pytest.raises(WatchRuleError):
        build_rules([{"kind": "regex", "pattern": "("}])
    with pytest.raises(WatchRuleError):
        build_rules([{"kind": "literal", "pattern": ""}])
    with pytest.raises(WatchRuleError):
        build_rules([{"kind": "glob", "pattern": "x"}])
    with pytest.raises(WatchRuleError, match="嵌套量词"):
        build_rules([{"kind": "regex", "pattern": "(a+)+$"}])


def test_catastrophic_regex_is_disabled_without_blocking_capture():
    matcher = WatchMatcher(
        build_rules([{"kind": "regex", "pattern": "(a|aa)+$", "notify": "once"}])
    )
    started = time.monotonic()
    update = matcher.feed("a" * 20000 + "!")
    elapsed = time.monotonic() - started
    # 墙钟预算生效：不阻塞采集（预算 0.25s，容忍调度抖动）
    assert elapsed < 5.0
    assert update.warnings or update.matches == []
    # 规则被禁用后继续喂数据不会再次尝试
    assert matcher.feed("still running").warnings == []
    matcher.close()


def test_window_is_bounded():
    matcher = WatchMatcher(
        build_rules([{"kind": "literal", "pattern": "needle"}]),
        window_chars=1024,
    )
    matcher.feed("x" * 5000)
    # 窗口被裁剪后仍能匹配新的完整内容
    update = matcher.feed("find needle here")
    assert len(update.matches) == 1
    matcher.close()
