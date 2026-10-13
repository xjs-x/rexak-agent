"""从 checkpoint、领域表与执行台账装配只读视图；不存第二份黑板。"""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.domain.leads import lead_summary
from agent_workbench.persistence.tables import LeadRow, ToolJobRow, ToolOperationRow

UNFINISHED = ("new", "investigating", "needs_verification", "blocked", "deferred")


def cursor_for(row: LeadRow) -> str:
    return f"{row.created_at.isoformat()}|{row.id}"


async def assemble_working_context(
    db: AsyncSession, session_id: UUID, state: dict[str, Any], *, limit: int = 4
) -> dict[str, Any]:
    """主线及最多三条备选，轮转使用全量范围的稳定键而非分数前 200 条。"""
    limit = max(2, min(limit, 4))
    focus = state.get("current_focus") or {}
    turn = int(state.get("completed_decision_rounds") or 0)
    base = select(LeadRow).where(
        LeadRow.session_id == session_id,
        LeadRow.status.in_(UNFINISHED),
        LeadRow.merged_into_id.is_(None),
    )
    selected: list[dict[str, Any]] = []
    ids: set[UUID] = set()

    def add(row: LeadRow, reason: str) -> None:
        if row.id not in ids and len(selected) < limit:
            selected.append({**lead_summary(row), "recall_reason": reason})
            ids.add(row.id)

    for raw in [focus.get("lead_id"), *(state.get("active_lead_ids") or [])[:limit]]:
        if not raw:
            continue
        try:
            row = (await db.execute(base.where(LeadRow.id == UUID(raw)))).scalar_one_or_none()
        except ValueError:
            continue
        if row is not None:
            add(row, "current_focus" if raw == focus.get("lead_id") else "retained_alternative")
    count = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    latest_change = (
        await db.execute(
            select(func.max(LeadRow.updated_at)).where(LeadRow.session_id == session_id)
        )
    ).scalar_one()
    source_signature = hashlib.sha256(
        json.dumps(
            {"focus": focus, "count": count, "latest_change": str(latest_change)}, sort_keys=True
        ).encode()
    ).hexdigest()
    # 发生方向/状态变化或每八个完成轮次回顾；新输入无旧候选也要选一页。
    previous = state.get("last_review_turn")
    retained_ids = {item["lead_id"] for item in selected}
    review = (
        previous is None
        or turn - int(previous) >= 8
        or not selected
        or state.get("last_review_source_signature") != source_signature
        or bool(set(state.get("active_lead_ids") or []) - retained_ids)
    )
    cursor = state.get("review_cursor")
    next_cursor = cursor
    if review and count:
        # 明确保留一个轮转位置，即使主线与保留候选已填满。
        if len(selected) >= limit:
            removed = selected.pop()
            ids.remove(UUID(removed["lead_id"]))
        rotating = base
        if cursor:
            timestamp, key = str(cursor).split("|", 1)
            at, lead_id = datetime.fromisoformat(timestamp), UUID(key)
            rotating = rotating.where(
                or_(LeadRow.created_at > at, and_(LeadRow.created_at == at, LeadRow.id > lead_id))
            )
        if ids:
            rotating = rotating.where(LeadRow.id.not_in(ids))
        row = (
            await db.execute(rotating.order_by(LeadRow.created_at, LeadRow.id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            wrapped = base.where(LeadRow.id.not_in(ids)) if ids else base
            row = (
                await db.execute(wrapped.order_by(LeadRow.created_at, LeadRow.id).limit(1))
            ).scalar_one_or_none()
        if row is not None:
            add(row, "stable_old_lead_rotation")
            next_cursor = cursor_for(row)
        query = str(focus.get("intent") or state.get("objective") or "").strip()[:128]
        related = base.where(LeadRow.hypothesis.contains(query, autoescape=True)) if query else base
        for candidate in (
            await db.execute(related.order_by(LeadRow.created_at, LeadRow.id).limit(limit))
        ).scalars():
            add(candidate, "keyword_related" if query else "initial_catalog")
    jobs = (
        (
            await db.execute(
                select(ToolJobRow)
                .join(ToolOperationRow, ToolOperationRow.operation_id == ToolJobRow.operation_id)
                .where(
                    ToolOperationRow.session_id == session_id,
                    ToolJobRow.state.in_(("received", "starting", "running", "unknown")),
                )
                .order_by(ToolJobRow.created_at, ToolJobRow.job_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "goal": {
            "objective": state.get("objective"),
            "constraints": state.get("user_constraints") or [],
        },
        "judgment": {"focus": focus, "leads": selected},
        "progress": {
            "plan_revision": int(state.get("plan_revision") or 0),
            "jobs": [
                {
                    "job_id": str(row.job_id),
                    "state": row.state,
                    "remote_revision": row.remote_revision,
                    "sync_status": row.sync_status,
                }
                for row in jobs[:20]
            ],
            "pending_job_count": len(jobs),
        },
        "next": {
            "unfinished_count": count,
            "selected_ids": [item["lead_id"] for item in selected],
            "review_due": review,
            "cursor_before": cursor,
            "cursor_after_selection": next_cursor,
            "turn": turn,
            "review_age_rounds": turn - int(previous or 0),
            "source_signature": source_signature,
            "note": "展示不代表模型已复评；游标在成功返回的请求后提交。",
        },
        "pending_job_ids": [str(row.job_id) for row in jobs],
    }
