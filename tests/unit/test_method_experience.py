"""经验来源和使用事件去重，项目与方法分支同时隔离。"""

from typing import Any
from uuid import uuid4

import pytest
from langgraph.store.memory import InMemoryStore

from agent_workbench.config.settings import Settings
from agent_workbench.memory.store import MemoryStore


async def test_trials_isolate_both_project_and_method_experiences():
    raw = InMemoryStore()
    a = MemoryStore(
        raw, Settings(owner_id="experiment", memory_read_branch="T-a", memory_write_branch="T-a")
    )
    b = MemoryStore(
        raw, Settings(owner_id="experiment", memory_read_branch="T-b", memory_write_branch="T-b")
    )
    await a.propose(
        project_id="same-project",
        memory_type="observation",
        content="项目候选",
        source_event_id="obs:1:1",
    )
    candidate = await a.propose(
        project_id="same-project",
        memory_type="method",
        memory_scope="method",
        content="先验证前置条件",
        applicable_conditions="操作失败",
        invalid_conditions="前置条件已独立确认",
        source_event_id="method:1:1",
    )
    repeat = await a.propose(
        project_id="same-project",
        memory_type="method",
        memory_scope="method",
        content="先验证前置条件",
        source_event_id="method:1:1",
    )
    assert candidate == repeat and repeat["verified_status"] == "candidate"
    assert len(await a.search(project_id="same-project")) == 1
    assert len(await a.list_methods()) == 1
    assert await b.search(project_id="same-project") == []
    assert await b.list_methods() == []
    with pytest.raises(ValueError, match="不同经验"):
        await a.propose(
            project_id="same-project",
            memory_type="method",
            memory_scope="method",
            content="同一事件修改了内容",
            source_event_id="method:1:1",
        )


async def test_usage_is_immutable_and_does_not_promote_candidate():
    raw = InMemoryStore()
    memory = MemoryStore(
        raw, Settings(owner_id="usage", memory_read_branch="trial", memory_write_branch="trial")
    )
    entry = await memory.propose(
        project_id="p", memory_type="method", memory_scope="method", content="条件复核"
    )
    args: dict[str, Any] = {
        "memory_id": entry["memory_id"],
        "source_event_id": "use:1",
        "source_run_id": uuid4(),
        "outcome": "helpful",
        "conditions": "在当前条件验证",
        "evidence_refs": ["actual-reference"],
    }
    first = await memory.record_usage(**args)
    assert await memory.record_usage(**args) == first
    assert (await memory.list_methods())[0]["verified_status"] == "candidate"
    events = await raw.asearch(("usage", "trials", "trial", "memory_usage"))
    assert len(events) == 1
    with pytest.raises(ValueError, match="更改反馈"):
        await memory.record_usage(**{**args, "outcome": "failed"})
    with pytest.raises(ValueError, match="不属于"):
        await memory.record_usage(**{**args, "memory_id": "unknown", "source_event_id": "use:2"})
