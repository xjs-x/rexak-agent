"""从 checkpoint、领域表与执行台账装配只读视图；不存第二份黑板。"""

import hashlib
import json
import re
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


def _review_triggers(state: dict[str, Any], focus: dict[str, Any]) -> list[str]:
    """明确的召回触发条件（§7.2）。

    正常轮转只是**兜底**，不能只依赖字符串精确包含整个目标句。
    """
    triggers: list[str] = []
    plan = list(state.get("plan") or [])
    focus_step = next(
        (step for step in plan if step.get("step_id") == (focus or {}).get("step_id")), None
    )
    if focus_step is not None and focus_step.get("status") in {"done", "dropped", "blocked"}:
        triggers.append("mainline_finished_or_blocked")
    assessment = state.get("work_assessment") or {}
    if assessment and int(assessment.get("revision") or 0) > 0:
        # 关键判断发生变化（假设被否定、条件不成立、需要换方向）。
        triggers.append("assessment_recorded")
    return triggers


async def assemble_working_context(
    db: AsyncSession, session_id: UUID, state: dict[str, Any], *, limit: int = 4
) -> dict[str, Any]:
    """主线及最多三条备选，轮转使用全量范围的稳定键而非分数前 200 条。

    召回优先级（定性分层，不做难以校准的总分调度）：
    当前焦点 -> 保留候选 -> 已有 step/lead/asset/identity 关系 -> 有限关键词 -> 稳定轮转。
    """
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

    retained_limit = max(1, limit - 2)
    for raw in [focus.get("lead_id"), *(state.get("active_lead_ids") or [])[:retained_limit]]:
        if len(selected) >= retained_limit:
            break
        if not raw:
            continue
        try:
            row = (await db.execute(base.where(LeadRow.id == UUID(raw)))).scalar_one_or_none()
        except ValueError:
            continue
        if row is not None:
            add(row, "current_focus" if raw == focus.get("lead_id") else "retained_alternative")

    # 计划步骤已绑定的线索：关系检索优先于关键词。
    plan_lead_ids = [
        str(step.get("lead_id"))
        for step in (state.get("plan") or [])
        if step.get("lead_id") and step.get("status") not in {"done", "dropped"}
    ]
    for raw in plan_lead_ids:
        if len(selected) >= limit - 1:
            break
        try:
            row = (await db.execute(base.where(LeadRow.id == UUID(raw)))).scalar_one_or_none()
        except ValueError:
            continue
        if row is not None:
            add(row, "plan_step_bound")

    # 身份关系检索：与已选线索共享身份集合的未完成线索。
    identity_values: list[str] = []
    asset_values: list[UUID] = []
    if selected:
        try:
            rows = (
                await db.execute(
                    select(LeadRow.identity_refs, LeadRow.asset_id).where(
                        LeadRow.id.in_([UUID(item["lead_id"]) for item in selected])
                    )
                )
            ).all()
        except ValueError:
            rows = []
        identity_values = sorted({ref for refs, _asset in rows for ref in (refs or []) if ref})
        asset_values = list({asset for _refs, asset in rows if asset is not None})
    if state.get("active_identity_id"):
        identity_values = sorted({*identity_values, str(state["active_identity_id"])})
    relationships = []
    if identity_values:
        relationships.append(
            or_(*(LeadRow.identity_refs.contains([identity]) for identity in identity_values))
        )
    if asset_values:
        relationships.append(LeadRow.asset_id.in_(asset_values))
    if relationships and len(selected) < limit - 1:
        related = base.where(or_(*relationships))
        if ids:
            related = related.where(LeadRow.id.not_in(ids))
        for candidate in (
            await db.execute(related.order_by(LeadRow.created_at, LeadRow.id).limit(limit))
        ).scalars():
            if len(selected) >= limit - 1:
                break
            add(candidate, "asset_or_identity_related")

    count = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    latest_change = (
        await db.execute(
            select(func.max(LeadRow.updated_at)).where(LeadRow.session_id == session_id)
        )
    ).scalar_one()
    assessment = state.get("work_assessment") or {}
    source_signature = hashlib.sha256(
        json.dumps(
            {
                "focus": focus,
                "count": count,
                "latest_change": str(latest_change),
                "assessment_revision": int(assessment.get("revision") or 0),
                "active_identity_id": state.get("active_identity_id"),
                "plan_revision": int(state.get("plan_revision") or 0),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    triggers = _review_triggers(state, focus)
    # 发生方向/状态变化、获得新判断、或每八个完成轮次回顾；新输入无旧候选也要选一页。
    previous = state.get("last_review_turn")
    retained_ids = {item["lead_id"] for item in selected}
    review = (
        previous is None
        or turn - int(previous) >= 8
        or not selected
        or state.get("last_review_source_signature") != source_signature
        or bool(set(state.get("active_lead_ids") or []) - retained_ids)
        or "mainline_finished_or_blocked" in triggers
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
        # 关键词兜底：只在关系检索不足时才使用，且不依赖整句精确包含。
        query = str(focus.get("intent") or state.get("objective") or "").strip()[:128]
        words = re.findall(r"[\w-]{2,}", query.casefold())[:6]
        related = (
            base.where(or_(*(LeadRow.hypothesis.ilike(f"%{word}%") for word in words)))
            if words
            else base
        )
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
            "review_triggers": triggers,
            "cursor_before": cursor,
            "cursor_after_selection": next_cursor,
            "turn": turn,
            "review_age_rounds": turn - int(previous or 0),
            "source_signature": source_signature,
            "note": (
                "展示不代表模型已复评；游标只在**召回结果实际发送**后才提交，"
                "因预算被省略的线索不算已展示。"
            ),
        },
        "pending_job_ids": [str(row.job_id) for row in jobs],
    }
