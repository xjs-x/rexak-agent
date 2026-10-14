"""结束复查集合与条件变化召回。

**本模块不 import agent**（方案 §8 依赖方向），只读业务表 + 显式传入的计划投影。

核心原则（方案 §6.1 / §6.2 / §7）：

1. 复查集合由**程序**从业务表枚举：所有未完成主线索、暂缓/受阻线索、合并后的未完成主对象、
   未完成计划事项、已登记的覆盖缺口，以及（P0 接入后）待分析弱点。
   模型的 `relevant_lead_ids` 只作为展示偏好，**不能**作为排除其他对象的依据。
2. 不使用 `.limit(200)` 当完整集合；每次只把少量待判断对象交回模型（有限分页）。
3. `blocked` / `deferred` **不在**自动终态集合中；缺失可推进性判断时归为 `needs_review`。
4. 条件变化召回先按**明确关系**召回，再检索同资产/身份与关键词候选；
   按"对象 + 实质版本 + 触发原因"去重。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import Actionability, CoverageStatus, LeadStatus
from agent_workbench.contracts.exploration import (
    REVIEW_PAGE_SIZE,
    ExploredRelationItem,
    RecallCandidate,
    ReviewInventory,
    ReviewInventoryItem,
)
from agent_workbench.contracts.weaknesses import connection_basis_fingerprint
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.weakness_relations import leads_affected_by_weakness, links_for_lead
from agent_workbench.persistence.tables import CoverageRow, FindingRow, LeadRow

#: 这些状态表示线索已经**结束**（有结果或已被判定排除）。
CLOSED_LEAD_STATUSES = frozenset(
    {str(LeadStatus.CONFIRMED), str(LeadStatus.DISPROVED)}
)


@dataclass(slots=True)
class ResolvedActionability:
    state: str
    source: str  # recorded | derived | stale
    note: str | None = None
    actionable: bool = False
    needs_review: bool = False
    missing_conditions: list[str] | None = None


def derive_actionability(row: LeadRow, *, stale: bool) -> ResolvedActionability:
    """解析线索当前的可推进性。

    规则（方案 §3.2 / §6.4）：

    - 已记录且依据未过期 -> 直接采用（`deferred + ready` 仍然算可推进）；
    - 已记录但依据过期 -> `needs_review`（**不**直接判成可执行或已解决）；
    - 无记录：存在未满足条件 -> `needs_condition`；有明确下一步 -> `ready`；
      其余（含没有任何依据的 `blocked`）-> `needs_review`。
    """
    stored = dict(row.actionability_assessment or {})
    if stored:
        if stale:
            return ResolvedActionability(
                state=str(Actionability.NEEDS_REVIEW),
                source="stale",
                note="来源弱点或条件已变化，原判断过期，需要重新评估。",
                needs_review=True,
            )
        state = str(stored.get("state") or Actionability.NEEDS_REVIEW)
        return ResolvedActionability(
            state=state,
            source="recorded",
            note=stored.get("note"),
            actionable=state == str(Actionability.READY),
            needs_review=state == str(Actionability.NEEDS_REVIEW),
        )

    conditions = list(row.required_conditions or [])
    missing = [
        str(item.get("description") or item.get("condition_key"))
        for item in conditions
        if not item.get("satisfied")
    ]
    if missing:
        return ResolvedActionability(
            state=str(Actionability.NEEDS_CONDITION),
            source="derived",
            missing_conditions=missing,
        )
    if (row.next_action or "").strip():
        return ResolvedActionability(
            state=str(Actionability.READY),
            source="derived",
            actionable=True,
        )
    # blocked / deferred 而没有可执行依据，或干脆没写下一步：一律需要复查。
    return ResolvedActionability(
        state=str(Actionability.NEEDS_REVIEW),
        source="derived",
        note="没有记录可推进性判断，也没有明确下一步。",
        needs_review=True,
    )


@dataclass(slots=True)
class _Candidate:
    key: str
    rank: int
    item: ReviewInventoryItem


async def build_review_inventory(
    session: AsyncSession,
    session_id: UUID,
    *,
    plan: Sequence[Mapping[str, Any]] | None = None,
    cursor: str | None = None,
    page_size: int = REVIEW_PAGE_SIZE,
    focus_lead_id: str | None = None,
) -> ReviewInventory:
    """枚举**完整**复查集合，并返回其中的一页。

    每次调用都重新枚举全量对象（因此"最终放行前再次检查全体对象"天然成立），
    分页只决定这一次交给模型的是哪几项。
    """
    candidates: list[_Candidate] = []
    rows = await _all_leads(session, session_id)
    stale_ids = await _stale_lead_ids(session, rows)

    actionable_count = 0
    unreviewed_count = 0
    for row in rows:
        resolution = derive_actionability(row, stale=row.id in stale_ids)
        if resolution.actionable:
            actionable_count += 1
        if resolution.needs_review:
            unreviewed_count += 1
        rank = 3
        if str(row.id) == focus_lead_id or resolution.actionable:
            rank = 0
        elif resolution.needs_review:
            rank = 1
        elif resolution.state == str(Actionability.NEEDS_CONDITION):
            rank = 2
        label = row.hypothesis[:120]
        detail = {
            "status": row.status,
            "priority": row.priority,
            "stale": row.id in stale_ids,
        }
        if resolution.missing_conditions:
            detail["missing_conditions"] = resolution.missing_conditions
        if resolution.note:
            detail["actionability_note"] = resolution.note
        candidates.append(
            _Candidate(
                key=f"lead:{row.id}",
                rank=rank,
                item=ReviewInventoryItem(
                    kind="lead",
                    object_id=str(row.id),
                    label=label,
                    status=row.status,
                    actionability=resolution.state,
                    stale=resolution.needs_review,
                    actionable=resolution.actionable,
                    detail=detail,
                ),
            )
        )

    # 未完成的计划事项（含覆盖检查、链条跟进与混合任务）。
    for step in plan or []:
        if step.get("status") in {"done", "dropped"}:
            continue
        candidates.append(
            _Candidate(
                key=f"plan_step:{step.get('step_id')}",
                rank=2,
                item=ReviewInventoryItem(
                    kind="plan_step",
                    object_id=str(step.get("step_id")),
                    label=str(step.get("description") or "")[:120],
                    status=str(step.get("status") or "pending"),
                    stale=False,
                    actionable=str(step.get("status")) == "in_progress",
                    detail={
                        "kind": step.get("kind") or "mixed",
                        "lead_ids": list(step.get("lead_ids") or []),
                        "rationale": step.get("rationale"),
                    },
                ),
            )
        )

    # 已登记的覆盖缺口（受阻项保留，不能靠改成 blocked 消除缺口）。
    coverage_rows = list(
        (
            await session.execute(
                select(CoverageRow).where(CoverageRow.session_id == session_id)
            )
        )
        .scalars()
        .all()
    )
    for row in coverage_rows:
        if row.status not in {
            str(CoverageStatus.NOT_CHECKED),
            str(CoverageStatus.BLOCKED),
        }:
            continue
        candidates.append(
            _Candidate(
                key=f"coverage_gap:{row.id}",
                rank=2 if row.status == str(CoverageStatus.BLOCKED) else 1,
                item=ReviewInventoryItem(
                    kind="coverage_gap",
                    object_id=str(row.id),
                    label=f"{row.entry_label or row.asset_id}:{row.check_category}"[:120],
                    status=row.status,
                    stale=False,
                    actionable=row.status == str(CoverageStatus.NOT_CHECKED),
                    detail={"reason": row.reason},
                ),
            )
        )

    # P0 接入：尚未判断后续用途（或判断已过期）的实质弱点。
    # **完整枚举**（keyset 分页）：不能用固定 limit 当完整集合，否则判据会被截断。
    pending = await FindingService(session).all_pending_connection_review(session_id)
    weakness_linked = {str(item) for item in await _weakness_ids_with_links(session, session_id)}
    for row in pending:
        has_link = str(row.id) in weakness_linked
        candidates.append(
            _Candidate(
                key=f"weakness:{row.id}",
                rank=1,
                item=ReviewInventoryItem(
                    kind="weakness",
                    object_id=str(row.id),
                    label=row.title[:120],
                    status=row.status,
                    actionability=None,
                    stale=True,
                    actionable=False,
                    detail={
                        "weakness_kind": row.weakness_kind,
                        "revision": row.revision,
                        "has_relation": has_link,
                        "condition_keys": [
                            str(item.get("key")) for item in (row.provided_conditions or [])
                        ],
                    },
                ),
            )
        )

    candidates.sort(key=lambda item: (item.rank, item.key))
    keys = [item.key for item in candidates]
    start = 0
    if cursor and cursor in keys:
        start = keys.index(cursor) + 1
    page = candidates[start : start + page_size]

    return ReviewInventory(
        session_id=str(session_id),
        items=[item.item for item in page],
        total_objects=len(candidates),
        unreviewed_count=unreviewed_count
        + sum(1 for item in candidates if item.item.kind == "weakness"),
        actionable_count=actionable_count
        + sum(
            1
            for item in candidates
            if item.item.kind == "coverage_gap" and item.item.actionable
        ),
        next_cursor=page[-1].key if page and start + page_size < len(candidates) else None,
        has_direction=any(item.item.actionable for item in candidates),
    )


async def _all_leads(session: AsyncSession, session_id: UUID) -> list[LeadRow]:
    """完整枚举（keyset 分页），不把 `.limit(200)` 当作完整集合。

    合并到主线索的**别名**不算独立任务：复查只针对规范主对象。
    """
    rows: list[LeadRow] = []
    cursor: UUID | None = None
    while True:
        selection = (
            select(LeadRow)
            .where(
                LeadRow.session_id == session_id,
                # 已合并别名不单独复查，否则同一任务会被算两次。
                LeadRow.merged_into_id.is_(None),
            )
            .order_by(LeadRow.created_at, LeadRow.id)
            .limit(200)
        )
        if cursor is not None:
            selection = selection.where(LeadRow.id > cursor)
        batch = list((await session.execute(selection)).scalars().all())
        if not batch:
            return rows
        rows.extend(batch)
        cursor = batch[-1].id


async def _stale_lead_ids(session: AsyncSession, rows: list[LeadRow]) -> set[UUID]:
    """依据指纹已过期的线索（条件或来源弱点版本变化）。"""
    from agent_workbench.domain.leads import LeadService

    service = LeadService(session)
    stale: set[UUID] = set()
    for row in rows:
        if not row.actionability_assessment:
            continue
        if await service.is_actionability_stale(row):
            stale.add(row.id)
    return stale


async def _weakness_ids_with_links(session: AsyncSession, session_id: UUID) -> list[UUID]:
    from agent_workbench.persistence.tables import LeadWeaknessLinkRow

    rows = (
        await session.execute(
            select(LeadWeaknessLinkRow.finding_id)
            .where(LeadWeaknessLinkRow.session_id == session_id)
            .distinct()
        )
    ).all()
    return [item[0] for item in rows]


# --------------------------------------------------------------------------- #
# 条件变化召回（P2）
# --------------------------------------------------------------------------- #


async def recall_by_condition_change(
    session: AsyncSession,
    session_id: UUID,
    *,
    new_weakness_ids: list[UUID] | None = None,
    revised_weakness_ids: list[UUID] | None = None,
    refuted_weakness_ids: list[UUID] | None = None,
    identity_changed: bool = False,
    mainline_blocked: bool = False,
    exclude_lead_ids: set[UUID] | None = None,
    limit: int = 5,
) -> list[RecallCandidate]:
    """条件变化触发的同会话召回。

    先精确 ID/条件关系，后资产/身份与关键词候选；相似文字**不自动**建立成立关系。
    """
    exclude = set(exclude_lead_ids or set())
    out: list[RecallCandidate] = []
    seen: set[str] = set()

    async def push(row: LeadRow, trigger: str, reason: str, relation_ids: list[str]) -> None:
        if row.id in exclude or row.status in CLOSED_LEAD_STATUSES:
            return
        dedupe = f"{row.id}:{row.revision}:{trigger}"
        if dedupe in seen:
            return
        seen.add(dedupe)
        resolution = derive_actionability(row, stale=False)
        out.append(
            RecallCandidate(
                lead_id=str(row.id),
                hypothesis=row.hypothesis[:200],
                status=row.status,
                actionability=resolution.state,
                recall_reason=reason,
                trigger=trigger,  # type: ignore[arg-type]
                dedupe_key=dedupe,
                relation_ids=relation_ids,
                missing_conditions=list(resolution.missing_conditions or []),
                revision=int(row.revision),
            )
        )

    for finding_id in list(new_weakness_ids or []) + list(revised_weakness_ids or []):
        trigger = "new_weakness" if finding_id in (new_weakness_ids or []) else "condition_revised"
        affected = await leads_affected_by_weakness(session, session_id, finding_id, exclude=exclude)
        for row in affected:
            links = await links_for_lead(session, session_id, row.id)
            await push(
                row,
                trigger,
                f"弱点 {finding_id} 的条件发生变化，该线索显式引用了它",
                [str(link.id) for link in links if link.finding_id == finding_id],
            )
        if len(out) >= limit:
            return out[:limit]

    for finding_id in refuted_weakness_ids or []:
        affected = await leads_affected_by_weakness(session, session_id, finding_id, exclude=exclude)
        for row in affected:
            # 弱点被反证：下游**标记待复核**，不级联判定全部线索 disproved。
            await push(
                row,
                "weakness_refuted",
                f"来源弱点 {finding_id} 被反证或适用范围收窄，需重新评估",
                [],
            )
        if len(out) >= limit:
            return out[:limit]

    if identity_changed and len(out) < limit:
        rows = (
            await session.execute(
                select(LeadRow)
                .where(
                    LeadRow.session_id == session_id,
                    LeadRow.merged_into_id.is_(None),
                )
                .order_by(LeadRow.created_at, LeadRow.id)
                .limit(limit * 4)
            )
        ).scalars()
        for row in rows:
            if not row.identity_refs:
                continue
            await push(row, "identity_changed", "身份或 auth_revision 发生变化，旧判断可能过期", [])
            if len(out) >= limit:
                break

    if mainline_blocked and len(out) < limit:
        rows = (
            await session.execute(
                select(LeadRow)
                .where(
                    LeadRow.session_id == session_id,
                    LeadRow.merged_into_id.is_(None),
                    LeadRow.status.notin_(
                        [str(LeadStatus.CONFIRMED), str(LeadStatus.DISPROVED)]
                    ),
                )
                .order_by(LeadRow.created_at, LeadRow.id)
                .limit(limit * 3)
            )
        ).scalars()
        for row in rows:
            if row.id in exclude:
                continue
            await push(row, "mainline_blocked", "主线受阻，重新排序其他未完成路线", [])
            if len(out) >= limit:
                break

    return out[:limit]


async def current_route(
    session: AsyncSession,
    session_id: UUID,
    *,
    focus_lead_id: str | None,
    limit: int = 6,
) -> list[ExploredRelationItem]:
    """当前主线的一跳关系（弱点 → 线索 → 弱点）。"""
    if not focus_lead_id:
        return []
    try:
        lead_id = UUID(focus_lead_id)
    except ValueError:
        return []
    links = await links_for_lead(session, session_id, lead_id)
    items: list[ExploredRelationItem] = []
    for link in links[:limit]:
        items.append(
            ExploredRelationItem(
                direction="basis" if link.role == "basis" else "result",
                link_id=str(link.id),
                lead_id=str(link.lead_id),
                weakness_id=str(link.finding_id),
                condition_key=link.condition_key,
                requirement_key=link.requirement_key,
                relation_state=link.relation_state,
                source_weakness_revision=link.source_weakness_revision,
                revision=int(link.revision),
            )
        )
    return items


async def weakness_condition_changes(
    session: AsyncSession,
    session_id: UUID,
    *,
    since_revision: dict[str, int] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """相对上次快照的条件/版本变化（供上下文呈现与诊断使用）。"""
    baseline = dict(since_revision or {})
    rows = list(
        (
            await session.execute(
                select(FindingRow)
                .where(FindingRow.session_id == session_id)
                .order_by(FindingRow.created_at, FindingRow.id)
                .limit(limit * 5)
            )
        )
        .scalars()
        .all()
    )
    changes: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.id)
        previous = baseline.get(key)
        if previous is None:
            changes.append(
                {
                    "weakness_id": key,
                    "change": "added",
                    "revision": int(row.revision),
                    "condition_keys": [
                        str(item.get("key")) for item in (row.provided_conditions or [])
                    ],
                    "weakness_kind": row.weakness_kind,
                    "title": row.title[:120],
                    "status": row.status,
                }
            )
        elif int(previous) != int(row.revision):
            changes.append(
                {
                    "weakness_id": key,
                    "change": "revised",
                    "revision": int(row.revision),
                    "previous_revision": int(previous),
                    "condition_keys": [
                        str(item.get("key")) for item in (row.provided_conditions or [])
                    ],
                    "weakness_kind": row.weakness_kind,
                    "title": row.title[:120],
                    "status": row.status,
                }
            )
        if len(changes) >= limit:
            break
    return changes


def weakness_review_fingerprint(row: FindingRow) -> str:
    """弱点用途判断的依据指纹（与 `FindingService._fingerprint` 保持一致）。"""
    stored = dict(row.connection_review or {})
    return connection_basis_fingerprint(
        weakness_id=str(row.id),
        content_revision=int(row.revision or 1),
        content_hash=str(row.content_hash or ""),
        lead_ids=[str(item) for item in (stored.get("linked_lead_ids") or [])],
        condition_keys=[str(item.get("key")) for item in (row.provided_conditions or [])],
    )


def sort_key_for_recall(item: RecallCandidate) -> tuple[int, str]:
    order = {
        "new_weakness": 0,
        "condition_revised": 1,
        "weakness_refuted": 2,
        "identity_changed": 3,
        "mainline_blocked": 4,
        "job_completed": 5,
    }
    return order.get(str(item.trigger), 9), item.lead_id


__all__ = [
    "CLOSED_LEAD_STATUSES",
    "ResolvedActionability",
    "build_review_inventory",
    "current_route",
    "derive_actionability",
    "recall_by_condition_change",
    "sort_key_for_recall",
    "weakness_condition_changes",
    "weakness_review_fingerprint",
]
