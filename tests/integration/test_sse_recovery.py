"""浏览器断线后的事件续读和过期游标恢复演练。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import delete

from agent_workbench.api.sse import event_stream
from agent_workbench.contracts.events import EventType
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import EventRow

pytestmark = pytest.mark.integration


def _frame_data(frame: str) -> dict[str, Any]:
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


async def test_sse_reconnect_replays_missing_events_and_expired_cursor_requests_resync(
    database, unique_name
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="sse-recovery-test",
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id,
            title="SSE 断线演练",
            scope_snapshot={},
            policy_snapshot={},
        )
        for number in range(1, 4):
            await append_event(
                db,
                session_id=task.id,
                event_type=EventType.RUN_STATUS,
                payload={"number": number},
            )

    # 第一个连接收到序号 1 后断开；写入序号 4 再重连，必须补回 2、3、4。
    first = cast(
        AsyncGenerator[str, None],
        event_stream(database.session_factory(), session_id=task.id, after_seq=0),
    )
    try:
        first_event = _frame_data(await asyncio.wait_for(anext(first), 3))
    finally:
        await first.aclose()
    assert first_event["session_seq"] == 1

    async with database.transaction() as db:
        await append_event(
            db, session_id=task.id, event_type=EventType.RUN_STATUS, payload={"number": 4}
        )

    resumed = cast(
        AsyncGenerator[str, None],
        event_stream(database.session_factory(), session_id=task.id, after_seq=1),
    )
    try:
        recovered = [
            _frame_data(await asyncio.wait_for(anext(resumed), 3))["session_seq"]
            for _ in range(3)
        ]
    finally:
        await resumed.aclose()
    assert recovered == [2, 3, 4]

    # 历史清理之后游标 1 已不可补读，服务端必须明确要求重新获取快照。
    async with database.transaction() as db:
        await db.execute(
            delete(EventRow).where(EventRow.session_id == task.id, EventRow.session_seq <= 2)
        )
    expired = cast(
        AsyncGenerator[str, None],
        event_stream(database.session_factory(), session_id=task.id, after_seq=1),
    )
    try:
        resync = _frame_data(await asyncio.wait_for(anext(expired), 3))
    finally:
        await expired.aclose()
    assert resync["type"] == "stream.resync_required"
    assert resync["payload"]["reason"] == "cursor_expired"
    assert resync["payload"]["earliest_available_seq"] == 3
    assert resync["payload"]["latest_seq"] == 4
