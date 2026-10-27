"""短事务内串行化同会话领域变更与最终结束检查，不持有额外状态。"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_exploration(session: AsyncSession, session_id: UUID) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"exploration:{session_id}"},
    )
