"""从 checkpoint、领域表与执行台账装配只读视图；不存第二份黑板。

V3（方案 §5.2 / §7）新增**弱点消费**区块：

```
changed_weaknesses: 本轮尚待分析的弱点摘要（含尚未建立任何关系的新弱点）
related_leads:      已显式关联、可能受本次变化影响的线索
condition_changes:  哪些条件增加、修订或失去依据
current_route:      当前主线的一跳关系、待解决条件和证据引用
pending_connection_review_count: 尚未判断后续用途的弱点数量
```

这些只是**只读投影**；"已保存 / 实际呈现 / 模型判断 / 关联执行"四件事分别记录，
只有真正进入最终请求 payload 的条目才推进呈现游标（由 context builder 的 manifest 证明）。
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.agent.state import plan_step_lead_ids
from agent_workbench.domain.exploration_review import (
    current_route,
    derive_actionability,
    recall_by_condition_change,
    sort_key_for_recall,
)
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService, lead_summary
from agent_workbench.persistence.tables import (
    FindingRow,
    IdentityRow,
    LeadRow,
    LeadWeaknessLinkRow,
    ToolJobRow,
    ToolOperationRow,
)

UNFINISHED = ("new", "investigating", "needs_verification", "blocked", "deferred")

#: 初始呈现预算（上下文资源初值，**不是**总探索数量上限）。
MAX_PRESENTED_WEAKNESSES = 2
MAX_RELATED_LEADS = 3
#: 候选池上限（用于挑选，不直接全部进入 payload）。
MAX_WEAKNESS_CANDIDATES = 12


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
        raw
        for step in (state.get("plan") or [])
        if step.get("status") not in {"done", "dropped"}
        for raw in plan_step_lead_ids(step)
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
    consumption = await _assemble_weakness_consumption(
        db, session_id, state, focus=focus, selected=selected
    )
    for item in [*selected, *consumption["related_leads"]]:
        row = await db.get(LeadRow, UUID(item["lead_id"]))
        if row is not None:
            resolution = derive_actionability(row, stale=bool(row.actionability_assessment) and await LeadService(db).is_actionability_stale(row))
            item["actionability"] = resolution.state
            if resolution.note:
                item["actionability_note"] = resolution.note
    step = _focus_step(state, focus)
    route_leads = set((step or {}).get("lead_ids") or [])
    if focus.get("lead_id"):
        route_leads.add(focus["lead_id"])
    source_versions = {}
    source_bindings = []
    if route_leads:
        sources = (await db.execute(select(LeadWeaknessLinkRow, FindingRow.revision)
            .join(FindingRow, FindingRow.id == LeadWeaknessLinkRow.finding_id)
            .where(LeadWeaknessLinkRow.session_id == session_id,
                LeadWeaknessLinkRow.lead_id.in_([UUID(raw) for raw in route_leads]),
                LeadWeaknessLinkRow.role == "basis", LeadWeaknessLinkRow.relation_state != "refuted"))).all()
        for link, current_revision in sources:
            key = str(link.finding_id)
            if link.source_weakness_revision is not None:
                source_versions[key] = min(source_versions.get(key, int(link.source_weakness_revision)), int(link.source_weakness_revision))
            source_bindings.append({"lead_id": str(link.lead_id), "weakness_id": key,
                "source_revision": link.source_weakness_revision, "current_revision": int(current_revision),
                "link_id": str(link.id), "relation_state": link.relation_state})
    return {
        # 派发绑定专用，不注入模型请求；涵盖步骤全部线索，不受可见一跳展示上限限制。
        "execution_sources": source_versions,
        "execution_source_bindings": source_bindings,
        "goal": {
            "objective": state.get("objective"),
            "constraints": state.get("user_constraints") or [],
        },
        "judgment": {
            "focus": focus,
            "leads": selected,
            "current_route": consumption["current_route"],
        },
        "weakness_consumption": consumption,
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
            "consumption_commit": consumption["consumption_commit"],
            "note": (
                "展示不代表模型已复评；游标只在**召回结果实际发送**后才提交，"
                "因预算被省略的线索不算已展示。"
            ),
        },
        "pending_job_ids": [str(row.job_id) for row in jobs],
    }


async def _assemble_weakness_consumption(
    db: AsyncSession,
    session_id: UUID,
    state: dict[str, Any],
    *,
    focus: dict[str, Any],
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    """弱点消费区块：变化弱点、相关线索、条件变化与当前路线。"""
    baseline = {
        str(key): int(value) for key, value in (state.get("last_weakness_revisions") or {}).items()
    }
    presented = {str(item) for item in (state.get("presented_weakness_ids") or [])}
    focus_lead_ids = {
        str(item.get("lead_id")) for item in selected if item.get("lead_id")
    } | ({str(focus.get("lead_id"))} if focus.get("lead_id") else set())

    rows = list(
        (
            await db.execute(
                select(FindingRow)
                .where(
                    FindingRow.session_id == session_id,
                )
                .order_by(FindingRow.created_at, FindingRow.id)
            )
        )
        .scalars()
        .all()
    )
    # 一次查全会话关系再分组：不能每个弱点各查一次（几百条弱点会变成几百次往返）。
    links_by_finding: dict[str, list[str]] = {}
    for link in (
        await db.execute(
            select(LeadWeaknessLinkRow).where(LeadWeaknessLinkRow.session_id == session_id)
        )
    ).scalars():
        links_by_finding.setdefault(str(link.finding_id), []).append(str(link.lead_id))
    service = FindingService(db)
    candidates: list[tuple[int, int, str, dict[str, Any]]] = []
    changed_conditions: list[dict[str, Any]] = []
    new_ids: list[UUID] = []
    revised_ids: list[UUID] = []
    pending_review_ids: list[str] = []
    for row in rows:
        key = str(row.id)
        previous = baseline.get(key)
        linked_lead_ids = links_by_finding.get(key, [])
        has_relation = key in links_by_finding
        # 尚未建立任何关系、也尚未判断用途的新弱点，必须有进入待分析列表的机会。
        needs_review = row.status != "disproved" and not service.review_is_current(row)
        if needs_review:
            pending_review_ids.append(key)
        if previous is None and needs_review:
            new_ids.append(row.id)
        elif previous is not None and int(previous) != int(row.revision):
            revised_ids.append(row.id)
        if not needs_review and (previous is None or int(previous) == int(row.revision)) and not (set(linked_lead_ids) & focus_lead_ids):
            continue
        if previous is None or int(previous) != int(row.revision):
            changed_conditions.append(
                {
                    "weakness_id": key,
                    "change": "added" if previous is None else "revised",
                    "condition_keys": [
                        str(item.get("key")) for item in (row.provided_conditions or [])
                    ],
                    "weakness_kind": row.weakness_kind,
                    "title": row.title[:120],
                    "status": row.status,
                }
            )
        # 定性优先级：当前主线相关 > 可能解除其他路线阻塞 > 其他待分析项。
        if set(linked_lead_ids) & focus_lead_ids:
            rank = 0
        elif row.provided_conditions:
            rank = 1
        else:
            rank = 2
        presented_rank = 1 if key in presented else 0
        candidates.append(
            (
                rank,
                presented_rank,
                key,
                {
                    "weakness_id": key,
                    "revision": int(row.revision),
                    "title": row.title[:160],
                    "status": row.status,
                    "weakness_kind": row.weakness_kind,
                    "change": "added" if previous is None else "revised",
                    "condition_keys": [
                        str(item.get("key")) for item in (row.provided_conditions or [])
                    ],
                    "provided_conditions": [
                        {
                            "key": str(item.get("key")),
                            "statement": str(item.get("statement") or "")[:200],
                            "evidence_refs": list(item.get("evidence_refs") or []),
                        }
                        for item in (row.provided_conditions or [])[:3]
                    ],
                    "linked_lead_ids": linked_lead_ids[:5],
                    "has_relation": has_relation,
                    "connection_review_required": needs_review,
                    "already_presented": key in presented,
                },
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    selected_weaknesses = [item[3] for item in candidates[:MAX_PRESENTED_WEAKNESSES]]

    # 条件变化召回（P2）：先精确关系，后资产/身份与主线受阻。
    mainline_blocked = str((_focus_step(state, focus) or {}).get("status")) in {
        "done",
        "dropped",
        "blocked",
    } or any(step.get("status") == "blocked" for step in state.get("plan") or [])
    active_identity = state.get("active_identity_id")
    identity = await db.get(IdentityRow, UUID(str(active_identity))) if active_identity else None
    identity_signature = f"{active_identity or ''}:{identity.auth_revision if identity else 0}"
    identity_changed = identity_signature != str(
        state.get("last_identity_signature") or ""
    )
    recall = await recall_by_condition_change(
        db,
        session_id,
        new_weakness_ids=new_ids,
        revised_weakness_ids=[key for key in revised_ids if next(row.status for row in rows if row.id == key) != "disproved"],
        refuted_weakness_ids=[row.id for row in rows if row.status == "disproved" and row.id in revised_ids],
        identity_changed=identity_changed,
        mainline_blocked=mainline_blocked,
        identity_signature=identity_signature,
        seen_keys={str(item) for item in (state.get("last_recall_keys") or [])},
        completed_operation_ids=list((await db.execute(select(ToolOperationRow.operation_id).where(
            ToolOperationRow.session_id == session_id, ToolOperationRow.job_id.is_not(None),
            ToolOperationRow.status.in_(("succeeded", "failed", "cancelled")),
        ).order_by(ToolOperationRow.updated_at.desc(), ToolOperationRow.operation_id))).scalars()),
        exclude_lead_ids={
            UUID(item["lead_id"]) for item in selected if item.get("lead_id")
        },
        limit=MAX_RELATED_LEADS * 2,
    )
    seen_keys = {str(item) for item in (state.get("last_recall_keys") or [])}
    recall = [item for item in recall if item.dedupe_key not in seen_keys]
    recall.sort(key=sort_key_for_recall)
    recall = recall[:MAX_RELATED_LEADS]

    related_leads: list[dict[str, Any]] = []
    for item in recall:
        related_leads.append(
            {
                "lead_id": item.lead_id,
                "hypothesis": item.hypothesis,
                "status": item.status,
                "actionability": item.actionability,
                "recall_reason": item.recall_reason,
                "trigger": item.trigger,
                "dedupe_key": item.dedupe_key,
                "missing_conditions": item.missing_conditions,
                "relation_ids": item.relation_ids,
                "revision": item.revision,
                "source": "condition_change_recall",
            }
        )
    # 显式关系补充：变化弱点的下游线索（即使没有触发召回也要让模型看到）。
    known = {item["lead_id"] for item in related_leads}
    for weakness in selected_weaknesses:
        for lead_id in weakness["linked_lead_ids"]:
            if len(related_leads) >= MAX_RELATED_LEADS or lead_id in known:
                continue
            row = (
                await db.execute(
                    select(LeadRow).where(
                        LeadRow.id == UUID(lead_id), LeadRow.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            related_leads.append(
                {**lead_summary(row), "source": "explicit_relation", "recall_reason": "显式关系"}
            )
            known.add(lead_id)

    route = await current_route(db, session_id, focus_lead_id=focus.get("lead_id"))
    route_missing: list[str] = []
    for item in route:
        row = await db.get(FindingRow, UUID(item.weakness_id))
        if row is None:
            continue
        for condition in row.provided_conditions or []:
            if item.condition_key and str(condition.get("key")) == item.condition_key:
                route_missing.append(str(condition.get("statement") or "")[:200])

    return {
        "changed_weaknesses": selected_weaknesses,
        "weakness_candidate_count": len(candidates),
        "weakness_scan_bounded": False,
        "related_leads": related_leads,
        "condition_changes": changed_conditions[:8],
        "current_route": [item.model_dump() for item in route],
        "unresolved_conditions": route_missing[:5],
        "pending_connection_review_count": len(pending_review_ids),
        "pending_connection_review_ids": pending_review_ids[:10],
        "new_weakness_ids": [str(item) for item in new_ids[:10]],
        "revised_weakness_ids": [str(item) for item in revised_ids[:10]],
        "identity_changed": identity_changed,
        "mainline_blocked": mainline_blocked,
        # 只有**真正进入最终 payload**的条目才提交这里（由 context builder 的 manifest 决定）。
        "consumption_commit": {
            "presented_weakness_ids": [item["weakness_id"] for item in selected_weaknesses],
            "recall_keys": [item["dedupe_key"] for item in related_leads if item.get("dedupe_key")],
            "weakness_revisions": {item["weakness_id"]: item["revision"] for item in selected_weaknesses},
            "identity_signature": identity_signature,
        },
        "presentation_budget": {
            "max_changed_weaknesses": MAX_PRESENTED_WEAKNESSES,
            "max_related_leads": MAX_RELATED_LEADS,
        },
        "note": (
            "这里只呈现有界摘要；候选池共 "
            f"{len(candidates)} 条。未进入最终请求 payload 的条目不算已呈现，"
            "也不会推进呈现游标。已呈现但没有判断的弱点仍是待判断项，"
            "`pending_connection_review_count` 会继续反映它们；"
            "结束复查由领域层完整枚举，**不**受这里的展示上限影响"
            "（`weakness_scan_bounded=true` 表示本次展示扫描已到上限）。"
        ),
    }


def _focus_step(state: dict[str, Any], focus: dict[str, Any]) -> dict[str, Any] | None:
    step_id = focus.get("step_id")
    if not step_id:
        return None
    for step in state.get("plan") or []:
        if step.get("step_id") == step_id:
            return step
    return None
