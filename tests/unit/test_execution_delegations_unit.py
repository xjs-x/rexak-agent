"""值班委托的确定性规则（验收 F05/F06/F11 的领域面）。

- 授权必须来自用户指令原话（确定性核对在集成测试里用真实指令验证）；
- 事件匹配有界：空过滤器不匹配任何事件（不能把任意字节都当唤醒）；
- 取消委托递增 revision，不触碰资源。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from agent_workbench.domain.execution_delegations import (
    ALLOWED_EVENT_FILTERS,
    AUTHORIZATION_SOURCES,
    delegation_digest,
    matches_event,
    normalize_quote,
)


def _delegation(**kwargs):
    base = {
        "id": uuid4(),
        "revision": 1,
        "mode": "once",
        "resource_ids": ["res-1"],
        "event_filters": ["resource.exited"],
    }
    base.update(kwargs)
    return cast(Any, SimpleNamespace(**base))


def test_matches_event_requires_explicit_scope():
    delegation = _delegation()
    assert matches_event(
        delegation, {"resource_id": "res-1", "type": "resource.exited"}
    )
    assert not matches_event(
        delegation, {"resource_id": "res-2", "type": "resource.exited"}
    )
    assert not matches_event(
        delegation, {"resource_id": "res-1", "type": "resource.watch_match"}
    )
    empty = _delegation(resource_ids=[], event_filters=[])
    assert not matches_event(empty, {"resource_id": "res-1", "type": "resource.exited"})


def test_normalize_quote_collapses_whitespace():
    assert normalize_quote("  持续\n处理\t 这个任务 ") == "持续 处理 这个任务"


def test_digest_changes_with_revision():
    delegation = _delegation()
    first = delegation_digest(delegation)
    delegation.revision = 2
    assert delegation_digest(delegation) != first


def test_authorization_sources_exclude_tool_output():
    # 工具输出/模板描述/终端文字都必须被拒绝：允许的来源只有这两类。
    assert set(AUTHORIZATION_SOURCES) == {"user_message", "ui_action"}
    assert "monitor_created" not in AUTHORIZATION_SOURCES
    assert "tool_output" not in AUTHORIZATION_SOURCES


def test_allowed_event_filters_cover_resource_lifecycle():
    for expected in (
        "resource.exited",
        "resource.watch_match",
        "resource.interaction_lost",
        "output.limit_reached",
    ):
        assert expected in ALLOWED_EVENT_FILTERS
