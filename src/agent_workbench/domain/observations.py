"""观察记录服务。

`观察` 是连接"工具结果"与"候选发现"的中间事实（§13.1）。
写入前必须通过**证据引用检查**：引用不存在或不可用的工件会被拒绝，
不允许"引用一个不存在的证据"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.persistence.tables import ObservationRow


async def record_observation_ref(
    session: AsyncSession,
    *,
    session_id: UUID,
    summary: str,
    artifact_ids: list[str],
    identity: str = "anonymous",
    url: str | None = None,
    source_operation_id: UUID | None = None,
) -> UUID:
    row = ObservationRow(
        id=uuid4(),
        session_id=session_id,
        summary=summary,
        identity_label=identity,
        url=url,
        artifact_ids=list(artifact_ids),
        source_operation_id=source_operation_id,
        observed_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row.id


async def list_observations(
    session: AsyncSession, session_id: UUID, *, limit: int = 200
) -> list[ObservationRow]:
    return list(
        (
            await session.execute(
                select(ObservationRow)
                .where(ObservationRow.session_id == session_id)
                .order_by(ObservationRow.observed_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
