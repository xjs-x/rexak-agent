"""完成的 run 才能将本 run 有可用证据的观察提炼为候选记忆。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from langgraph.store.postgres.aio import AsyncPostgresStore

from agent_workbench.domain.observations import record_observation_ref
from agent_workbench.memory.extract import distill_completed_run
from agent_workbench.memory.store import MemoryStore
from agent_workbench.persistence.bootstrap import open_store_connection
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import ArtifactRow, RunRow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completed_run_distills_only_its_available_observations(database, settings) -> None:
    project_name = f"memory-distill-{uuid.uuid4().hex}"
    run_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=settings.owner_id,
            name=project_name,
            target_description="本地记忆提炼测试",
            default_scope={},
        )
        test_session = await SessionRepository(db).create(
            project_id=project.id,
            title="测试会话",
            scope_snapshot={},
            policy_snapshot={},
        )
        db.add(
            RunRow(
                id=run_id,
                session_id=test_session.id,
                status="completed",
                objective="提炼观察",
                provider_id="fake",
                model_name="fake",
            )
        )
        now = datetime.now(UTC)
        for item_id, availability in ((artifact_id, "available"), (orphan_id, "orphaned")):
            db.add(
                ArtifactRow(
                    id=item_id,
                    sha256="0" * 64,
                    byte_size=1,
                    media_type="text/plain",
                    relative_key=f"test/{item_id}",
                    session_id=test_session.id,
                    availability=availability,
                    captured_at=now,
                )
            )
        await db.flush()
        await record_observation_ref(
            db,
            session_id=test_session.id,
            source_run_id=run_id,
            summary="入口返回 200，但这本身不是漏洞",
            artifact_ids=[str(artifact_id)],
            url="http://127.0.0.1:8899/",
        )
        await record_observation_ref(
            db,
            session_id=test_session.id,
            source_run_id=run_id,
            summary="不可用证据不应沉淀",
            artifact_ids=[str(orphan_id)],
            url="http://127.0.0.1:8899/orphan",
        )
        await record_observation_ref(
            db,
            session_id=test_session.id,
            summary="不属于本 run 的旧观察",
            artifact_ids=[str(artifact_id)],
            url="http://127.0.0.1:8899/old",
        )

    assert await distill_completed_run(database, settings, run_id) == 1
    async with open_store_connection(settings) as conn:
        memory = MemoryStore(AsyncPostgresStore(conn), settings)
        entries = await memory.search(project_id=project.id)
    assert len(entries) == 1
    assert entries[0]["verified_status"] == "candidate"
    assert entries[0]["source_artifact_ids"] == [str(artifact_id)]
    assert "不属于本 run" not in entries[0]["content"]
