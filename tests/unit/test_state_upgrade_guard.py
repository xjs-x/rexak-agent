"""旧状态和读取故障必须在模型与工具执行前停止。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_workbench.agent.state import (
    StateSchemaIncompatibleError,
    assert_compatible_state,
)
from agent_workbench.persistence.bootstrap import (
    BusinessSchemaMismatchError,
    assert_business_revisions,
    expected_business_revisions,
)
from agent_workbench.runtime.runner import AgentRunner


def test_state_schema_requires_an_explicit_version() -> None:
    assert_compatible_state({})
    assert_compatible_state({"state_schema_version": 2, "messages": ["已保存"]})
    for state in (
        {"messages": ["旧状态"]},
        {"state_schema_version": 0, "messages": ["旧状态"]},
        {"state_schema_version": 3, "messages": ["未来状态"]},
        {"state_schema_version": 1, "messages": ["尚未适配"]},
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
    assert len(expected) == 1
    assert_business_revisions(expected, expected)
    with pytest.raises(BusinessSchemaMismatchError, match="业务库 schema 版本不匹配"):
        assert_business_revisions({"older-revision"}, expected)
    with pytest.raises(BusinessSchemaMismatchError, match="业务库 schema 版本不匹配"):
        assert_business_revisions({"future-revision"}, expected)
    with pytest.raises(BusinessSchemaMismatchError, match="业务库 schema 版本不匹配"):
        assert_business_revisions(set(), expected)
