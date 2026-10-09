"""事件表：状态变更与对应事件的**同一业务事务**提交。

一致性算法（BUILD PLAN §12.2）：
1. 在短事务内对 `sessions` 行加锁（`SELECT ... FOR UPDATE`）；
2. 读取并递增 `sessions.event_seq`，得到本事务的 `session_seq`；
3. 在同一事务内写入 `events` 行；
4. 提交。**提交后才能广播**。

因为序号分配与事件写入在同一事务、且被 session 行锁串行化，
所以"先提交的事件序号一定小于后提交的"，普通序列号（sequence/identity）在
事务乱序提交时会让游标跳过未提交事件的问题在这里不会发生。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.events import EVENT_SCHEMA_VERSION, EventEnvelope, EventType
from agent_workbench.persistence.tables import EventRow, SessionRow


class SessionNotFoundError(LookupError):
    pass


async def allocate_session_seq(session: AsyncSession, session_id: UUID) -> int:
    """在**当前事务内**分配下一个 session 事件序号。

    必须由调用方在同一个事务里调用 `append_event`，否则序号会空洞。
    `with_for_update()` 让同一 session 的并发写者串行，保证提交顺序与序号一致。
    """
    row = (
        await session.execute(
            select(SessionRow).where(SessionRow.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise SessionNotFoundError(f"session 不存在：{session_id}")
    row.event_seq = int(row.event_seq) + 1
    await session.flush()
    return int(row.event_seq)


async def append_event(
    session: AsyncSession,
    *,
    session_id: UUID,
    event_type: EventType,
    payload: dict[str, Any] | None = None,
    run_id: UUID | None = None,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> EventEnvelope:
    """在**当前事务**内追加一条事件。

    调用方负责提交。禁止在提交之前广播（否则客户端可能先看到不存在的状态）。
    """
    moment = occurred_at or datetime.now(UTC)
    seq = await allocate_session_seq(session, session_id)
    row = EventRow(
        event_id=event_id or uuid4(),
        session_id=session_id,
        session_seq=seq,
        run_id=run_id,
        type=str(event_type),
        schema_version=EVENT_SCHEMA_VERSION,
        payload=payload or {},
        occurred_at=moment,
    )
    session.add(row)
    await session.flush()
    return EventEnvelope(
        event_id=row.event_id,
        session_seq=row.session_seq,
        session_id=row.session_id,
        run_id=row.run_id,
        type=EventType(row.type),
        occurred_at=row.occurred_at,
        payload=row.payload,
    )


async def read_events_after(
    session: AsyncSession,
    *,
    session_id: UUID,
    after_seq: int = 0,
    limit: int = 500,
) -> list[EventEnvelope]:
    """按 `session_seq > after_seq` 顺序补读。"""
    rows = (
        (
            await session.execute(
                select(EventRow)
                .where(and_(EventRow.session_id == session_id, EventRow.session_seq > after_seq))
                .order_by(EventRow.session_seq)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_to_envelope(row) for row in rows]


async def latest_seq(session: AsyncSession, session_id: UUID) -> int:
    value = (
        await session.execute(
            select(func.coalesce(func.max(EventRow.session_seq), 0)).where(
                EventRow.session_id == session_id
            )
        )
    ).scalar_one()
    return int(value)


async def min_available_seq(session: AsyncSession, session_id: UUID) -> int | None:
    """当前保留的最小序号。用于判断客户端游标是否已过期（需要 resync）。"""
    value = (
        await session.execute(
            select(func.min(EventRow.session_seq)).where(EventRow.session_id == session_id)
        )
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def _to_envelope(row: EventRow) -> EventEnvelope:
    return EventEnvelope(
        schema_version=row.schema_version,
        event_id=row.event_id,
        session_seq=row.session_seq,
        session_id=row.session_id,
        run_id=row.run_id,
        type=EventType(row.type),
        occurred_at=row.occurred_at,
        payload=row.payload or {},
    )


class EventLog:
    """事件写入器的轻量封装，供领域服务在同一事务内使用。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._pending: list[EventEnvelope] = []

    async def emit(
        self,
        *,
        session_id: UUID,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        run_id: UUID | None = None,
    ) -> EventEnvelope:
        envelope = await append_event(
            self._session,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            run_id=run_id,
        )
        self._pending.append(envelope)
        return envelope

    def drain(self) -> Sequence[EventEnvelope]:
        """提交成功后调用，取出待广播事件。"""
        pending = tuple(self._pending)
        self._pending.clear()
        return pending
