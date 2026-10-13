"""按业务记录判断真实进展与结果相似，避免模型重复同一条无效路线。

三个概念**分开**（§7.1）：

| 信号 | 数据来源 | 用法 |
|---|---|---|
| execution activity | 操作数量、状态、请求摘要、运行中 job | 告诉用户在做事；**不算**知识增量 |
| knowledge changes | 观察/线索/发现的有效版本变化、明确判断及其来源 | 候选的信息变化信号 |
| result similarity | 同一焦点下结果状态、规范化类别、重复错误 | 触发温和提醒；**不是**无价值判决 |

要点：
- 新的 `request_digest` 不再直接进入**知识进展签名**（换参数不等于新知识）；
- 新增观察数量也不等价于新知识；对完全相同来源和内容的重复写入不重置提醒；
- 结果相似不能只用完整 body hash（时间戳等动态内容会扰动），首版采用保守的
  状态/类型归类加来源引用，并**标注置信不足**；不确定是否相似就保持 unknown。
"""

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
#: 同一焦点下连续出现相似结果的回合数（起始调参值，只影响提醒节奏）。
SIMILAR_RESULT_STREAK = 3
#: 相似判定的置信度：首版不引入独立语义判别模型，因此明确标为低。
RESULT_SIMILARITY_CONFIDENCE = "low"

INTERACTION_TOOLS = (
    "http_request",
    "browser_navigate",
    "browser_action",
    "run_command",
    "read_workspace_file",
)

#: 固定短句：具体、可执行，不阻止合法请求，也不自动结束任务。
SIMILAR_RESULT_FEEDBACK = (
    "最近几次结果相似。它们否定了哪个假设？如果测试条件尚未成立，"
    "先做一个能区分条件问题与路径不通的最小验证；仍有证据支撑就继续，"
    "核心假设被否定再换方向。"
)


async def semantic_progress_signature(db: AsyncSession, session_id: Any) -> str:
    """知识进展签名：只使用去重后的**领域事实**。

    新请求参数（`request_digest`）不进这里 —— 换请求不等于获得新知识。
    """
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


async def activity_signature(db: AsyncSession, session_id: Any) -> str:
    """执行活动签名：**只反映"在做事"**，不计入知识增量。"""
    operations = (
        (
            await db.execute(
                select(ToolOperationRow.tool_name, ToolOperationRow.status).where(
                    ToolOperationRow.session_id == session_id
                )
            )
        )
        .all()
    )
    canonical = "\n".join(sorted(f"{name}:{status}" for name, status in operations)).encode()
    return hashlib.sha256(canonical).hexdigest()


async def similar_result_signature(
    db: AsyncSession, session_id: Any, focus_step_id: str | None
) -> str:
    """结果相似签名（保守版）。

    只使用"工具名 + 终态"和当前焦点；**不含** `request_digest`（否则换参数就变）。
    动态内容（时间戳等）不参与，因此缺少语义判别时置信度标为 low。
    """
    rows = (
        (
            await db.execute(
                select(ToolOperationRow.tool_name, ToolOperationRow.status).where(
                    ToolOperationRow.session_id == session_id,
                    ToolOperationRow.tool_name.in_(INTERACTION_TOOLS),
                    ToolOperationRow.status.in_(("succeeded", "failed", "cancelled", "unknown")),
                )
            )
        )
        .all()
    )
    counters: dict[str, int] = {}
    for name, status in rows:
        counters[f"{name}:{status}"] = counters.get(f"{name}:{status}", 0) + 1
    payload = {
        "focus_step_id": focus_step_id,
        "classes": sorted(counters.items()),
        "confidence": RESULT_SIMILARITY_CONFIDENCE,
    }
    return hashlib.sha256(str(payload).encode()).hexdigest()


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


def next_similarity_state(
    previous_signature: str,
    previous_streak: int,
    current_signature: str,
) -> dict[str, Any]:
    """相似结果计数。中性命名 `similar_result_streak`，不宣称"无进展"。"""
    if not previous_signature or previous_signature != current_signature:
        return {"last_similar_signature": current_signature, "similar_result_streak": 0}
    return {
        "last_similar_signature": current_signature,
        "similar_result_streak": previous_streak + 1,
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
        focus = state.get("current_focus") or {}
        async with context.toolbox.db.session() as db:
            current = await semantic_progress_signature(db, context.toolbox.session_id)
            similar = await similar_result_signature(
                db, context.toolbox.session_id, focus.get("step_id")
            )
        update = next_progress_state(
            str(state.get("last_progress_signature") or ""),
            int(state.get("no_progress_streak") or 0),
            current,
        )
        update["last_progress_turn"] = turn
        update["progress_feedback"] = None
        update.update(
            next_similarity_state(
                str(state.get("last_similar_signature") or ""),
                int(state.get("similar_result_streak") or 0),
                similar,
            )
        )
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
        if (
            update["similar_result_streak"] >= SIMILAR_RESULT_STREAK
            and turn - int(state.get("last_similar_reminder_turn") or -8)
            >= REMINDER_COOLDOWN_ROUNDS
        ):
            # 相似结果提醒更具体，优先级高于泛化的"没有变化"提示。
            update["last_similar_reminder_turn"] = turn
            update["progress_feedback"] = SIMILAR_RESULT_FEEDBACK
        return update
