"""旧事件清理只影响可重建的事件流，不影响活跃任务与游标恢复。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select, update

from agent_workbench.api.sse import event_stream
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import create_run
from agent_workbench.maintenance.retention import prune_old_events
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import EventRow, RunRow

pytestmark = pytest.mark.integration


def _frame_data(frame: str) -> dict[str, Any]:
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


async def test_event_retention_preserves_active_runs_and_resyncs_old_cursor(
    database, unique_name
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="event-retention-test", name=unique_name,
            target_description="本地目标", default_scope={},
        )
        inactive = await SessionRepository(db).create(
            project_id=project.id, title="可清理", scope_snapshot={}, policy_snapshot={},
        )
        active = await SessionRepository(db).create(
            project_id=project.id, title="运行中", scope_snapshot={}, policy_snapshot={},
        )
        active_run = await create_run(
            db, session_id=active.id, objective="仍在执行", provider_id="test",
            model_name="test", config_snapshot={},
        )
        for target in (inactive, active):
            for number in range(1, 4):
                await append_event(
                    db, session_id=target.id, event_type=EventType.RUN_STATUS,
                    payload={"number": number},
                )
        await db.execute(
            update(EventRow).where(EventRow.session_id.in_([inactive.id, active.id]))
            .values(created_at=old)
        )

    async with database.transaction() as db:
        assert await prune_old_events(
            db, days=30, limit=100, now=now, session_id=inactive.id
        ) == 2
        assert await prune_old_events(
            db, days=30, limit=100, now=now, session_id=active.id
        ) == 0
    async with database.transaction() as db:
        assert await prune_old_events(
            db, days=30, limit=1, apply=True, now=now, session_id=inactive.id
        ) == 1
    async with database.transaction() as db:
        assert await prune_old_events(
            db, days=30, limit=100, apply=True, now=now, session_id=inactive.id
        ) == 1

    async with database.session() as db:
        remaining = (
            await db.execute(
                select(EventRow.session_id, EventRow.session_seq)
                .where(EventRow.session_id.in_([inactive.id, active.id]))
                .order_by(EventRow.session_id, EventRow.session_seq)
            )
        ).all()
    assert [seq for sid, seq in remaining if sid == inactive.id] == [3]
    assert [seq for sid, seq in remaining if sid == active.id] == [1, 2, 3]

    async with database.transaction() as db:
        await db.execute(
            update(RunRow).where(RunRow.id == active_run.id).values(status="completed")
        )
        assert await prune_old_events(
            db, days=30, limit=100, apply=True, now=now, session_id=active.id
        ) == 2
    async with database.session() as db:
        active_sequences = (
            await db.execute(
                select(EventRow.session_seq).where(EventRow.session_id == active.id)
            )
        ).scalars().all()
    assert active_sequences == [3]

    stream = cast(
        AsyncGenerator[str, None],
        event_stream(database.session_factory(), session_id=inactive.id, after_seq=1),
    )
    try:
        frame = _frame_data(await asyncio.wait_for(anext(stream), 3))
    finally:
        await stream.aclose()
    assert frame["type"] == "stream.resync_required"
    assert frame["payload"]["earliest_available_seq"] == 3
    assert frame["payload"]["latest_seq"] == 3
