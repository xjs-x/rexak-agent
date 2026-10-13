"""跨任务经验的项目隔离、候选状态和同主体冲突。"""

import pytest
from langgraph.store.memory import InMemoryStore

from agent_workbench.config.settings import Settings
from agent_workbench.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_conflicting_candidate_cannot_overwrite_verified_project_fact() -> None:
    store = MemoryStore(
        InMemoryStore(),
        Settings(owner_id="owner-a", memory_read_branch=None, memory_write_branch=None),
    )  # type: ignore[arg-type]
    first = await store.propose(
        project_id="project-a",
        memory_type="endpoint_behavior",
        subject_key="https://example.test/status",
        content="状态接口返回 200",
        source_run_id="run-1",
        source_artifact_ids=["artifact-1"],
    )
    await store.promote_to_verified(
        project_id="project-a",
        memory_type="endpoint_behavior",
        content="状态接口返回 200",
        verified_by_run_id="run-1",
        evidence_refs=["artifact-1"],
    )
    second = await store.propose(
        project_id="project-a",
        memory_type="endpoint_behavior",
        subject_key="https://example.test/status",
        content="状态接口返回 403",
        source_run_id="run-2",
        source_artifact_ids=["artifact-2"],
    )
    assert second["verified_status"] == "candidate"
    assert first["memory_id"] in second["conflict"]["conflicts_with"]

    current = await store.search(project_id="project-a", limit=10)
    assert {item["verified_status"] for item in current} == {"verified", "candidate"}
    assert await store.search(project_id="project-b", limit=10) == []
