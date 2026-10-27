"""事件流保留策略。

事件是供界面续读的投影，不是报告、证据或模型调用台账。每个任务始终
保留最新事件作为游标锚点；活跃 run 的任务完全跳过。清理只通过显式命令执行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import ACTIVE_RUN_STATUSES
from agent_workbench.persistence.tables import EventRow, RunRow, SessionGraphRow, SessionRow


async def retention_candidates(
    session: AsyncSession, *, before: datetime, limit: int, session_id: UUID | None = None
) -> list[int]:
    """选取可清理的旧事件 ID；每个 session 至少保留最新事件。"""
    if before.tzinfo is None or before.utcoffset() != timedelta(0):
        raise ValueError("before 必须是 UTC 时间")
    if limit < 1:
        raise ValueError("limit 必须大于零")
    active_run = exists(
        select(RunRow.id).where(
            RunRow.session_id == EventRow.session_id,
            RunRow.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    stmt = (
        select(EventRow.id)
        .join(SessionRow, SessionRow.id == EventRow.session_id)
        .where(
            EventRow.created_at < before,
            EventRow.session_seq < SessionRow.event_seq,
            ~active_run,
            ~unreviewed_graph_event(),
        )
        .order_by(EventRow.created_at, EventRow.id)
        .limit(limit)
    )
    if session_id is not None:
        stmt = stmt.where(EventRow.session_id == session_id)
    return list((await session.execute(stmt)).scalars().all())


async def prune_old_events(
    session: AsyncSession,
    *,
    days: int,
    limit: int = 1000,
    apply: bool = False,
    now: datetime | None = None,
    session_id: UUID | None = None,
) -> int:
    """预览或删除至多 limit 个旧事件，返回数量；调用方管理事务。"""
    if days < 1:
        raise ValueError("days 必须大于零")
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("now 必须是 UTC 时间")
    cutoff = moment - timedelta(days=days)
    ids = await retention_candidates(session, before=cutoff, limit=limit, session_id=session_id)
    if apply and ids:
        # 再次检查运行状态：预览查询之后，新 run 可能恰好开始。
        active_run = exists(
            select(RunRow.id).where(
                RunRow.session_id == EventRow.session_id,
                RunRow.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        deleted = await session.execute(
            delete(EventRow)
            .where(EventRow.id.in_(ids), EventRow.created_at < cutoff, ~active_run, ~unreviewed_graph_event())
            .returning(EventRow.id)
        )
        return len(deleted.scalars().all())
    return len(ids)


def unreviewed_graph_event():
    """Session 记忆尚未核对的事件不是可清理的纯 UI 投影。"""
    return exists(select(SessionGraphRow.session_id).where(
        SessionGraphRow.session_id == EventRow.session_id,
        SessionGraphRow.reviewed_seq < EventRow.session_seq,
    ))
