"""线索—弱点关系：同会话写入、版本核对、邻居检索与影响范围查询。

设计约束（方案 §3.2 / §3.3 / §4.2）：

- `basis` 表示线索从哪个弱点/条件获得依据；`result` 表示验证实际产生/更新了哪个弱点；
- `evidence_linked` 只表示**关系**已有引用依据，不等于整条组合路径已确认；
- **两个弱点分别成立不代表其组合已成立**：组合关系保持假设，不继承 confirmed；
- 关系身份在 `(lead_id, finding_id, role, condition_key, requirement_key)` 上唯一；
- 关系可有回路（回路本身不能作为证据）；线索别名合并不得形成循环。

本模块**不 import agent**，可被领域层与其他领域服务直接复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import (
    ErrorCode,
    LeadWeaknessRelationState,
    LeadWeaknessRole,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.weaknesses import WeaknessRelationRef
from agent_workbench.persistence.tables import (
    FindingRow,
    LeadRow,
    LeadWeaknessLinkRow,
)


@dataclass(slots=True)
class LinkWriteResult:
    link_id: UUID
    lead_id: UUID
    finding_id: UUID
    role: str
    relation_state: str
    created: bool
    changed: bool
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": str(self.link_id),
            "lead_id": str(self.lead_id),
            "finding_id": str(self.finding_id),
            "role": self.role,
            "relation_state": self.relation_state,
            "created": self.created,
            "changed": self.changed,
            "revision": self.revision,
        }


@dataclass(slots=True)
class RelationBatchResult:
    links: list[LinkWriteResult] = field(default_factory=list)
    refuted: list[LinkWriteResult] = field(default_factory=list)

    @property
    def affected_weakness_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for item in [*self.links, *self.refuted]:
            seen.setdefault(str(item.finding_id), None)
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "links": [item.to_dict() for item in self.links],
            "refuted": [item.to_dict() for item in self.refuted],
            "affected_weakness_ids": self.affected_weakness_ids,
        }


async def upsert_links(
    session: AsyncSession,
    *,
    session_id: UUID,
    lead_id: UUID,
    refs: list[WeaknessRelationRef],
    source_operation_id: UUID | None = None,
    relation_state: LeadWeaknessRelationState | None = None,
) -> RelationBatchResult:
    """在同一事务内写入/更新关系。调用方负责事务边界（失败整体撤销）。"""
    result = RelationBatchResult()
    lead = await _require_lead(session, session_id, lead_id)
    for ref in refs:
        role = ref.role_enum
        finding = await _require_finding(session, session_id, UUID(ref.weakness_id))
        if ref.source_weakness_revision is not None and int(
            ref.source_weakness_revision
        ) != int(finding.revision):
            raise ApiError(
                ErrorCode.CONFLICT,
                f"弱点 {ref.weakness_id} 已被更新到第 {finding.revision} 版，"
                f"本次基于第 {ref.source_weakness_revision} 版的关系写入已过期；"
                "旧版本来源不自动升级为新版本判断，请重新读取后再提交。",
                details={"current_revision": finding.revision},
            )
        if ref.condition_key:
            known = {str(item.get("key")) for item in (finding.provided_conditions or [])}
            if ref.condition_key not in known:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    f"弱点 {ref.weakness_id} 没有条件 {ref.condition_key}；"
                    "请先记录该条件，再建立引用它的关系。",
                )
        state = relation_state or (
            LeadWeaknessRelationState.EVIDENCE_LINKED
            if ref.evidence_refs
            else LeadWeaknessRelationState.PROPOSED
        )
        row = await _find_existing(session, lead.id, finding.id, role, ref)
        if row is None:
            row = LeadWeaknessLinkRow(
                id=uuid4(),
                session_id=session_id,
                lead_id=lead.id,
                finding_id=finding.id,
                role=str(role),
                condition_key=ref.condition_key,
                requirement_key=ref.requirement_key,
                source_weakness_revision=int(finding.revision),
                relation_state=str(state),
                evidence_refs=list(ref.evidence_refs),
                source_operation_id=source_operation_id,
                revision=1,
            )
            session.add(row)
            result.links.append(
                LinkWriteResult(
                    row.id, lead.id, finding.id, str(role), str(state), True, True, 1
                )
            )
        else:
            merged = _merge(row.evidence_refs, ref.evidence_refs)
            changed = merged != list(row.evidence_refs or [])
            new_state = str(
                _upgrade_state(row.relation_state, state, has_evidence=bool(merged))
            )
            if new_state != row.relation_state:
                changed = True
            if changed:
                row.evidence_refs = merged
                row.relation_state = new_state
                row.source_weakness_revision = int(finding.revision)
                row.revision = int(row.revision) + 1
                if source_operation_id is not None:
                    row.source_operation_id = source_operation_id
                session.add(row)
            result.links.append(
                LinkWriteResult(
                    row.id,
                    lead.id,
                    finding.id,
                    str(role),
                    str(row.relation_state),
                    False,
                    changed,
                    int(row.revision),
                )
            )
    await session.flush()
    return result


async def refute_links(
    session: AsyncSession,
    *,
    session_id: UUID,
    lead_id: UUID,
    weakness_ids: list[str],
) -> list[LinkWriteResult]:
    """把指定关系标记为 `refuted`（弱点被反证）：**不**级联判定下游线索失败。"""
    lead = await _require_lead(session, session_id, lead_id)
    parsed: list[UUID] = []
    for raw in weakness_ids:
        try:
            parsed.append(UUID(str(raw)))
        except ValueError as exc:
            raise ApiError(ErrorCode.VALIDATION_FAILED, f"弱点 ID 非法：{raw}") from exc
    if not parsed:
        return []
    rows = list(
        (
            await session.execute(
                select(LeadWeaknessLinkRow).where(
                    LeadWeaknessLinkRow.session_id == session_id,
                    LeadWeaknessLinkRow.lead_id == lead.id,
                    LeadWeaknessLinkRow.finding_id.in_(parsed),
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[LinkWriteResult] = []
    for row in rows:
        if row.relation_state == str(LeadWeaknessRelationState.REFUTED):
            continue
        row.relation_state = str(LeadWeaknessRelationState.REFUTED)
        row.revision = int(row.revision) + 1
        session.add(row)
        out.append(
            LinkWriteResult(
                row.id,
                row.lead_id,
                row.finding_id,
                row.role,
                row.relation_state,
                False,
                True,
                int(row.revision),
            )
        )
    await session.flush()
    return out


async def links_for_lead(session: AsyncSession, session_id: UUID, lead_id: UUID) -> list[LeadWeaknessLinkRow]:
    return list(
        (
            await session.execute(
                select(LeadWeaknessLinkRow)
                .where(
                    LeadWeaknessLinkRow.session_id == session_id,
                    LeadWeaknessLinkRow.lead_id == lead_id,
                )
                .order_by(LeadWeaknessLinkRow.created_at, LeadWeaknessLinkRow.id)
            )
        )
        .scalars()
        .all()
    )


async def mark_dependent_judgements_stale(
    session: AsyncSession, session_id: UUID, finding_id: UUID
) -> list[UUID]:
    """来源弱点的实质内容变化后，把依赖它的**已记录**判断降到 `needs_review`。

    只标记待复核，**不**级联判定线索失败，也不猜测新的结论（方案 §3.2 / §7）。
    """
    from agent_workbench.contracts.weaknesses import actionability_basis_fingerprint

    links = await links_for_finding(session, session_id, finding_id)
    touched: list[UUID] = []
    for link in links:
        lead = await session.get(LeadRow, link.lead_id)
        if lead is None:
            continue
        stored = dict(lead.actionability_assessment or {})
        if not stored:
            continue
        basis: list[tuple[str, int | None]] = []
        for item in await links_for_lead(session, session_id, lead.id):
            finding = await session.get(FindingRow, item.finding_id)
            basis.append((str(item.finding_id), int(finding.revision) if finding else None))
        lead.actionability_assessment = {
            **stored,
            "state": "needs_review",
            "basis_fingerprint": actionability_basis_fingerprint(
                hypothesis=lead.hypothesis,
                next_action=lead.next_action,
                required_conditions=list(lead.required_conditions or []),
                basis_revisions=basis,
                identity_state="|".join(sorted(str(item) for item in (lead.identity_refs or [])))
                or None,
            ),
            "revision": int(stored.get("revision") or 0) + 1,
        }
        session.add(lead)
        touched.append(lead.id)
    await session.flush()
    return touched


async def links_for_finding(
    session: AsyncSession, session_id: UUID, finding_id: UUID
) -> list[LeadWeaknessLinkRow]:
    return list(
        (
            await session.execute(
                select(LeadWeaknessLinkRow)
                .where(
                    LeadWeaknessLinkRow.session_id == session_id,
                    LeadWeaknessLinkRow.finding_id == finding_id,
                )
                .order_by(LeadWeaknessLinkRow.created_at, LeadWeaknessLinkRow.id)
            )
        )
        .scalars()
        .all()
    )


async def links_for_session(
    session: AsyncSession,
    session_id: UUID,
    *,
    weakness_id: UUID | None = None,
    lead_id: UUID | None = None,
    limit: int = 200,
    after_id: UUID | None = None,
) -> list[LeadWeaknessLinkRow]:
    selection = select(LeadWeaknessLinkRow).where(LeadWeaknessLinkRow.session_id == session_id)
    if weakness_id is not None:
        selection = selection.where(LeadWeaknessLinkRow.finding_id == weakness_id)
    if lead_id is not None:
        selection = selection.where(LeadWeaknessLinkRow.lead_id == lead_id)
    if after_id is not None:
        selection = selection.where(LeadWeaknessLinkRow.id > after_id)
    return list(
        (
            await session.execute(
                selection.order_by(LeadWeaknessLinkRow.created_at, LeadWeaknessLinkRow.id).limit(
                    limit
                )
            )
        )
        .scalars()
        .all()
    )


async def leads_affected_by_weakness(
    session: AsyncSession,
    session_id: UUID,
    finding_id: UUID,
    *,
    exclude: set[UUID] | None = None,
    limit: int = 50,
) -> list[LeadRow]:
    """该弱点显式关联的线索。

    只按**明确关系**返回（先精确关系、后检索候选），供条件变化召回使用。
    """
    selection = (
        select(LeadRow)
        .join(LeadWeaknessLinkRow, LeadWeaknessLinkRow.lead_id == LeadRow.id)
        .where(
            LeadWeaknessLinkRow.session_id == session_id,
            LeadWeaknessLinkRow.finding_id == finding_id,
            LeadRow.merged_into_id.is_(None),
        )
        .order_by(LeadRow.created_at, LeadRow.id)
        .limit(limit)
    )
    rows = list((await session.execute(selection)).scalars().all())
    if exclude:
        rows = [row for row in rows if row.id not in exclude]
    return rows


async def weakens_with_condition_reference(
    session: AsyncSession, session_id: UUID, condition_key: str
) -> list[LeadWeaknessLinkRow]:
    return list(
        (
            await session.execute(
                select(LeadWeaknessLinkRow).where(
                    LeadWeaknessLinkRow.session_id == session_id,
                    or_(
                        LeadWeaknessLinkRow.condition_key == condition_key,
                        LeadWeaknessLinkRow.requirement_key == condition_key,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )


async def repoint_links(
    session: AsyncSession, session_id: UUID, source_lead_id: UUID, target_lead_id: UUID
) -> int:
    """线索合并时把关系改挂到**规范主线索**，并去重。

    返回被改挂的关系数量。合并已在 `LeadService.merge` 内串行化。
    """
    rows = await links_for_lead(session, session_id, source_lead_id)
    moved = 0
    for row in rows:
        existing = await _find_existing(
            session,
            target_lead_id,
            row.finding_id,
            LeadWeaknessRole(row.role),
            _ProxyRef(row.condition_key, row.requirement_key),
        )
        if existing is None:
            row.lead_id = target_lead_id
            row.revision = int(row.revision) + 1
            session.add(row)
            moved += 1
        else:
            merged = _merge(existing.evidence_refs, row.evidence_refs)
            if merged != list(existing.evidence_refs or []):
                existing.evidence_refs = merged
                existing.revision = int(existing.revision) + 1
                session.add(existing)
                moved += 1
            await session.delete(row)
    await session.flush()
    return moved


class _ProxyRef:
    __slots__ = ("condition_key", "requirement_key")

    def __init__(self, condition_key: str | None, requirement_key: str | None) -> None:
        self.condition_key = condition_key
        self.requirement_key = requirement_key


async def _find_existing(
    session: AsyncSession,
    lead_id: UUID,
    finding_id: UUID,
    role: LeadWeaknessRole,
    ref: Any,
) -> LeadWeaknessLinkRow | None:
    condition = (
        LeadWeaknessLinkRow.condition_key.is_(None)
        if ref.condition_key is None
        else LeadWeaknessLinkRow.condition_key == ref.condition_key
    )
    requirement = (
        LeadWeaknessLinkRow.requirement_key.is_(None)
        if ref.requirement_key is None
        else LeadWeaknessLinkRow.requirement_key == ref.requirement_key
    )
    return (
        await session.execute(
            select(LeadWeaknessLinkRow).where(
                LeadWeaknessLinkRow.lead_id == lead_id,
                LeadWeaknessLinkRow.finding_id == finding_id,
                LeadWeaknessLinkRow.role == str(role),
                condition,
                requirement,
            )
        )
    ).scalar_one_or_none()


async def _require_lead(session: AsyncSession, session_id: UUID, lead_id: UUID) -> LeadRow:
    row = (
        await session.execute(
            select(LeadRow).where(LeadRow.id == lead_id, LeadRow.session_id == session_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"线索不存在或不属于当前会话：{lead_id}")
    return row


async def _require_finding(session: AsyncSession, session_id: UUID, finding_id: UUID) -> FindingRow:
    row = (
        await session.execute(
            select(FindingRow).where(
                FindingRow.id == finding_id, FindingRow.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"弱点不存在或不属于当前会话：{finding_id}")
    return row


def _upgrade_state(
    current: str, incoming: LeadWeaknessRelationState, *, has_evidence: bool
) -> LeadWeaknessRelationState:
    existing = LeadWeaknessRelationState(current)
    if existing is LeadWeaknessRelationState.REFUTED:
        # 被反证的关系不因重复写入自动复活；需要重新评估后显式更新。
        return existing
    if incoming is LeadWeaknessRelationState.REFUTED:
        return incoming
    if has_evidence:
        return LeadWeaknessRelationState.EVIDENCE_LINKED
    return existing


def _merge(left: list[str] | None, right: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for item in list(left or []) + list(right or []):
        seen.setdefault(str(item), None)
    return list(seen)


__all__ = [
    "LinkWriteResult",
    "RelationBatchResult",
    "leads_affected_by_weakness",
    "links_for_finding",
    "links_for_lead",
    "links_for_session",
    "mark_dependent_judgements_stale",
    "refute_links",
    "repoint_links",
    "upsert_links",
    "weakens_with_condition_reference",
]
