"""按业务记录判断真实进展，避免模型重复同一条无效路线。"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.signals import NoProgressBlocked
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.tables import (
    ArtifactRow,
    AssetRow,
    CoverageRow,
    FindingRow,
    LeadRow,
)

# 只针对连续重复观察，不按时长或费用截断任务。
STRATEGY_FEEDBACK_STREAK = 3
PAUSE_STREAK = 8


async def semantic_progress_signature(db: AsyncSession, session_id: Any) -> str:
    """使用去重后的事实；重复写同一证据/入口不算进展。"""
    facts: set[str] = set()
    artifacts = (
        (
            await db.execute(
                select(ArtifactRow.sha256).where(
                    ArtifactRow.session_id == session_id,
                    ArtifactRow.availability == "available",
                    ArtifactRow.truncated.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    facts.update(f"evidence:{sha}" for sha in artifacts)

    assets = (
        (await db.execute(select(AssetRow).where(AssetRow.session_id == session_id)))
        .scalars()
        .all()
    )
    facts.update(
        f"asset:{row.scheme}:{row.host}:{row.port}:{row.method}:{row.path}:"
        f"{','.join(sorted(row.query_keys or []))}:{row.identity_key}"
        for row in assets
    )
    leads = (
        (await db.execute(select(LeadRow).where(LeadRow.session_id == session_id))).scalars().all()
    )
    facts.update(
        f"lead:{row.hypothesis.strip().casefold()}:{row.status}:{row.asset_id}:"
        f"{row.blocked_reason or ''}"
        for row in leads
    )
    coverage = (
        (
            await db.execute(
                select(CoverageRow).where(
                    CoverageRow.session_id == session_id,
                )
            )
        )
        .scalars()
        .all()
    )
    facts.update(
        f"coverage:{row.asset_id}:{row.entry_label}:{row.identity_id}:"
        f"{row.check_category}:{row.status}"
        for row in coverage
    )
    confirmed = (
        (
            await db.execute(
                select(FindingRow).where(
                    FindingRow.session_id == session_id,
                    FindingRow.status == "confirmed",
                )
            )
        )
        .scalars()
        .all()
    )
    facts.update(f"verified:{row.id}:{row.verification_id}" for row in confirmed)
    canonical = "\n".join(sorted(facts)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def next_progress_state(
    previous_signature: str,
    previous_streak: int,
    current_signature: str,
) -> dict[str, Any]:
    if not previous_signature or previous_signature != current_signature:
        return {"last_progress_signature": current_signature, "no_progress_streak": 0}
    return {
        "last_progress_signature": current_signature,
        "no_progress_streak": previous_streak + 1,
    }


class ProgressMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = runtime
        streak = int(state.get("no_progress_streak") or 0)
        if streak >= PAUSE_STREAK:
            raise NoProgressBlocked(streak)
        return

    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context: RunContext | None = getattr(runtime, "context", None)
        if context is None:
            return None
        async with context.toolbox.db.session() as db:
            current = await semantic_progress_signature(db, context.toolbox.session_id)
        return next_progress_state(
            str(state.get("last_progress_signature") or ""),
            int(state.get("no_progress_streak") or 0),
            current,
        )
