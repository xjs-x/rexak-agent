"""旧状态和读取故障必须在模型与工具执行前停止。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_workbench.agent.state import (
    CURRENT_STATE_SCHEMA_VERSION,
    StateSchemaIncompatibleError,
    assert_compatible_state,
)
from agent_workbench.agent.state_migration import _apply_update
from agent_workbench.persistence.bootstrap import (
    BusinessSchemaMismatchError,
    assert_business_revisions,
    expected_business_revisions,
)
from agent_workbench.runtime.runner import AgentRunner


def test_state_schema_requires_an_explicit_version() -> None:
    assert_compatible_state({})
    assert_compatible_state({"state_schema_version": CURRENT_STATE_SCHEMA_VERSION, "messages": ["已保存"]})
    for state in (
        {"messages": ["旧状态"]},
        {"state_schema_version": 0, "messages": ["旧状态"]},
        {"state_schema_version": CURRENT_STATE_SCHEMA_VERSION + 1, "messages": ["未来状态"]},
        # v1/v2/v3 必须先经过显式迁移，不能直接当当前版本加载。
        {"state_schema_version": 1, "messages": ["尚未适配"]},
        {"state_schema_version": 2, "messages": ["尚未适配"]},
        {"state_schema_version": 3, "messages": ["尚未适配"]},
        {"state_schema_version": 4, "messages": ["尚未适配"]},
        {"state_schema_version": True, "messages": ["错误类型"]},
    ):
        with pytest.raises(StateSchemaIncompatibleError, match="拒绝恢复"):
            assert_compatible_state(state)


async def test_runner_never_treats_checkpoint_read_failure_as_fresh_input() -> None:
    async def failed_read(_config: Any) -> Any:
        raise OSError("checkpoint 不可达")

    runner = AgentRunner()
    runtime = SimpleNamespace(graph=SimpleNamespace(aget_state=failed_read))
    run = SimpleNamespace(id=uuid4(), session_id=uuid4(), objective="初始目标")
    task = SimpleNamespace(thread_id="existing-thread")
    with pytest.raises(RuntimeError, match="拒绝重复注入目标"):
        await runner._segment_input(cast(Any, runtime), cast(Any, run), cast(Any, task), 1)


async def test_runner_refuses_unversioned_checkpoint_before_resuming() -> None:
    async def old_state(_config: Any) -> Any:
        return SimpleNamespace(values={"messages": ["历史"]})

    runner = AgentRunner()
    runtime = SimpleNamespace(graph=SimpleNamespace(aget_state=old_state))
    run = SimpleNamespace(id=uuid4(), session_id=uuid4(), objective="初始目标")
    task = SimpleNamespace(thread_id="existing-thread")
    with pytest.raises(StateSchemaIncompatibleError):
        await runner._segment_input(cast(Any, runtime), cast(Any, run), cast(Any, task), 1)


def test_business_schema_version_rejects_older_and_future_revisions() -> None:
    expected = expected_business_revisions()
    assert len(expected) == 2
    assert expected == {"b903eaf18201", "c416a8d2e016"}
    assert_business_revisions(expected, expected)
    with pytest.raises(BusinessSchemaMismatchError, match="业务库 schema 版本不匹配"):
        assert_business_revisions({"older-revision"}, expected)
    with pytest.raises(BusinessSchemaMismatchError, match="业务库 schema 版本不匹配"):
        assert_business_revisions({"future-revision"}, expected)
    with pytest.raises(BusinessSchemaMismatchError, match="业务库 schema 版本不匹配"):
        assert_business_revisions(set(), expected)


class _StubGraph:
    """模拟 `aupdate_state` 在不同 `as_node` 下会改变 `next` 的图。

    真实暂停会话会停在 `next=('model',)`；默认推断（`as_node=None`）会把它算成
    "没有待执行任务"，等于吞掉一次待执行的模型调用。迁移必须换候选重试并验证。
    """

    def __init__(self, working: tuple[str | None, ...]) -> None:
        self.working = working
        self.calls: list[str | None] = []

    def get_graph(self) -> Any:
        return SimpleNamespace(nodes=["__start__", "model", "tools", "__end__"])

    async def aupdate_state(self, config: Any, update: dict[str, Any], as_node: Any = None) -> Any:
        self.calls.append(as_node)
        self.working_now = as_node in self.working
        return {"configurable": {"thread_id": "t", "checkpoint_id": f"ck-{len(self.calls)}"}}

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(
            values={"messages": [], "state_schema_version": 4},
            next=("model",) if self.working_now else (),
        )


async def test_migration_keeps_pending_node_even_when_default_as_node_drops_it() -> None:
    stub = _StubGraph(working=("model", "__start__"))
    snapshot = SimpleNamespace(
        values={"messages": [], "state_schema_version": 3},
        next=("model",),
        config={"configurable": {"thread_id": "t", "checkpoint_id": "v3"}},
        metadata={"writes": {}},
    )
    migrated = await _apply_update(stub, snapshot, {"state_schema_version": 4}, snapshot.config)
    assert tuple(migrated.next) == ("model",), "待执行节点必须保留"
    assert stub.calls[0] is None, "先按框架默认推断尝试"
    assert any(call in {"model", "__start__"} for call in stub.calls[1:]), "失败后必须换候选重试"


async def test_migration_refuses_when_no_candidate_preserves_pending_node() -> None:
    stub = _StubGraph(working=())
    snapshot = SimpleNamespace(
        values={"messages": [], "state_schema_version": 3},
        next=("model",),
        config={"configurable": {"thread_id": "t", "checkpoint_id": "v3"}},
        metadata={"writes": {}},
    )
    with pytest.raises(StateSchemaIncompatibleError, match="拒绝恢复"):
        await _apply_update(stub, snapshot, {"state_schema_version": 4}, snapshot.config)
    assert len(stub.calls) >= 3, "所有候选都试过才拒绝"
