"""按业务记录判断真实进展，避免模型重复同一条无效路线。"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.segment import completed_model_message
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.tables import (
    AssetRow,
    CoverageRow,
    FindingRow,
    LeadRow,
    ObservationRow,
    ToolOperationRow,
)

# 只针对连续重复观察，不按时长或费用截断任务。
STRATEGY_FEEDBACK_STREAK = 3
PAUSE_STREAK = 8
REMINDER_COOLDOWN_ROUNDS = 8


async def semantic_progress_signature(db: AsyncSession, session_id: Any) -> str:
    """使用去重后的事实；重复写同一证据/入口不算进展。"""
    facts: set[str] = set()
    observations = (
        (await db.execute(select(ObservationRow).where(ObservationRow.session_id == session_id)))
        .scalars()
        .all()
    )
    facts.update(
        f"observation:{row.summary.strip()}:{row.url}:{row.identity_label}:"
        f"{','.join(sorted(row.artifact_ids or []))}"
        for row in observations
    )
    operations = (
        (
            await db.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.session_id == session_id,
                    ToolOperationRow.tool_name.in_(
                        (
                            "http_request",
                            "browser_navigate",
                            "browser_action",
                            "run_command",
                            "read_workspace_file",
                        )
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    facts.update(
        f"operation:{row.tool_name}:{row.request_digest}:{row.status}" for row in operations
    )

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
        f"{row.blocked_reason or ''}:{row.next_action or ''}:{row.merged_into_id or ''}"
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
        context: RunContext | None = getattr(runtime, "context", None)
        if context is None:
            return None
        turn = int(state.get("completed_decision_rounds") or 0)
        # 初始轮取基线；后续只在已闭合轮次之后计算，不累计后台等待或重入。
        if state.get("last_progress_signature") and (
            completed_model_message(dict(state)) is None or state.get("last_progress_turn") == turn
        ):
            return {"progress_feedback": None}
        async with context.toolbox.db.session() as db:
            current = await semantic_progress_signature(db, context.toolbox.session_id)
        update = next_progress_state(
            str(state.get("last_progress_signature") or ""),
            int(state.get("no_progress_streak") or 0),
            current,
        )
        update["last_progress_turn"] = turn
        update["progress_feedback"] = None
        if (
            update["no_progress_streak"] >= STRATEGY_FEEDBACK_STREAK
            and turn - int(state.get("last_progress_reminder_turn") or -8)
            >= REMINDER_COOLDOWN_ROUNDS
        ):
            update["last_progress_reminder_turn"] = turn
            update["progress_feedback"] = (
                f"连续 {update['no_progress_streak']} 个完成轮次没有观察到测试知识变化。"
                "这是软提示，不能据此认定探索无价值或结束。区分未测到、条件不成立和结果不支持；"
                "检查历史成果、受阻线索和重复候选，调整下一步；有意复查时简短说明新条件。"
            )
        return update
