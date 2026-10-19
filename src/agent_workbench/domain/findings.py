"""发现、验证与覆盖；Finding 同时是**弱点**实体。

生命周期（§13.1）：
```
工具返回 -> 观察 -> 候选发现 -> 验证记录 -> confirmed / disproved / inconclusive
```

硬约束：
- **模型/工具只能写 candidate**；`confirmed` 必须由**验证记录**支撑
  （数据库 CHECK：`status <> 'confirmed' OR verification_id IS NOT NULL`）；
- 区分 `execution_success` / `finding_verified` / `goal_answered` 三种结论；
- 阴性结论必须附测试条件，不宣称永久排除；
- 覆盖率只有在**分母明确**时展示，不能称为"网站漏洞覆盖率"。

弱点扩展（方案 §3.1）：
- 复用 `findings` 表，**不**另建同步写入的 Weakness 主表；
- `provided_conditions` 的证据引用说明判断依据，**不**自动证明陈述成立；
- 父弱点已 confirmed **也**不能使新增条件或组合关系自动 confirmed；
- 条件 key 由本服务在幂等事务内分配；同内容重复更新**不**推进知识版本；
- 只有实质内容变化才递增 `revision`；仅写"已分析后续用途"元数据不递增。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import (
    CoverageStatus,
    ErrorCode,
    FindingCategory,
    FindingStatus,
    WeaknessKind,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.weaknesses import (
    MAX_CONDITIONS_PER_WRITE,
    ApplicabilitySpec,
    ConnectionReview,
    ProvidedCondition,
    canonical_condition_key,
    connection_basis_fingerprint,
    content_revision_payload,
)
from agent_workbench.domain.coverage_checklist import (
    CoverageProfile,
    build_checklist,
    reconcile_checklist,
)
from agent_workbench.persistence.tables import (
    AssetRow,
    CoverageProfileRow,
    CoverageRow,
    FindingRow,
    IdentityRow,
    LeadRow,
    ParameterRow,
    SessionRow,
    VerificationRow,
)


@dataclass(slots=True)
class WeaknessWriteResult:
    """弱点写入的有界回执：**不**返回长原始正文。"""

    weakness_id: UUID
    revision: int
    created: bool
    content_changed: bool
    condition_keys: list[str] = field(default_factory=list)
    added_condition_keys: list[str] = field(default_factory=list)
    revised_condition_keys: list[str] = field(default_factory=list)
    removed_condition_keys: list[str] = field(default_factory=list)
    review_required: bool = False
    review_stale: bool = False
    linked_lead_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weakness_id": str(self.weakness_id),
            "revision": self.revision,
            "created": self.created,
            "content_changed": self.content_changed,
            "condition_keys": self.condition_keys,
            "added_condition_keys": self.added_condition_keys,
            "revised_condition_keys": self.revised_condition_keys,
            "removed_condition_keys": self.removed_condition_keys,
            "connection_review_required": self.review_required,
            "connection_review_stale": self.review_stale,
            "linked_lead_ids": self.linked_lead_ids,
        }


class FindingService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def propose(
        self,
        *,
        session_id: UUID,
        title: str,
        description: str,
        category: FindingCategory = FindingCategory.OTHER,
        asset_id: UUID | None = None,
        identity_id: UUID | None = None,
        evidence_refs: list[str] | None = None,
        impact: str | None = None,
    ) -> FindingRow:
        """登记**候选**发现。永远不能直接写 confirmed。"""
        row = FindingRow(
            id=uuid4(),
            session_id=session_id,
            title=title,
            description=description,
            category=str(category),
            asset_id=asset_id,
            identity_id=identity_id,
            status=str(FindingStatus.CANDIDATE),
            impact=impact,
            evidence_refs=list(evidence_refs or []),
            weakness_kind=str(WeaknessKind.UNCLASSIFIED),
            applicability={},
            provided_conditions=[],
        )
        row.content_hash = _content_hash(row)
        self._s.add(row)
        await self._s.flush()
        return row

    async def upsert_weakness(
        self,
        *,
        session_id: UUID,
        weakness_id: UUID | None = None,
        expected_revision: int | None = None,
        title: str | None = None,
        description: str | None = None,
        category: str | None = None,
        asset_id: UUID | None = None,
        identity_id: UUID | None = None,
        impact: str | None = None,
        weakness_kind: str | None = None,
        applicability: dict[str, Any] | None = None,
        provided_conditions: list[dict[str, Any]] | None = None,
        remove_condition_keys: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        connection_review: dict[str, Any] | None = None,
    ) -> WeaknessWriteResult:
        """创建或补充弱点；首次创建与后续补充走**同一**实现。

        - `None` 表示保留已有值；显式空值清空（`provided_conditions=[]`）；
        - 只允许创建 candidate，不接受 confirmed；
        - 指定 `expected_revision` 时，过期写入返回明确冲突，不覆盖新的证据和条件。
        """
        from agent_workbench.domain.exploration_lock import lock_exploration

        await lock_exploration(self._s, session_id)
        if weakness_id is None:
            clean_title = (title or "").strip()
            if not clean_title:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "创建弱点必须提供 title")
            row = FindingRow(
                id=uuid4(),
                session_id=session_id,
                title=clean_title,
                description=description or "",
                category=str(category or FindingCategory.OTHER),
                asset_id=asset_id,
                identity_id=identity_id,
                status=str(FindingStatus.CANDIDATE),
                impact=impact,
                evidence_refs=list(evidence_refs or []),
                weakness_kind=_coerce_kind(weakness_kind),
                applicability=ApplicabilitySpec.normalize(applicability).model_dump(),
                provided_conditions=[],
            )
            self._s.add(row)
            created = True
        else:
            row = await self.get(weakness_id)
            if row is None or row.session_id != session_id:
                raise ApiError(ErrorCode.NOT_FOUND, f"弱点不存在：{weakness_id}")
            if expected_revision is not None and int(row.revision) != int(expected_revision):
                raise ApiError(
                    ErrorCode.CONFLICT,
                    f"弱点 {weakness_id} 已被更新到第 {row.revision} 版，"
                    f"本次基于第 {expected_revision} 版的写入已过期；请重新读取后再提交。",
                    details={"current_revision": row.revision},
                )
            created = False
            # 已确认记录的核心事实不原地扩大：新事实请建候选记录再建立关系。
            if row.status == str(FindingStatus.CONFIRMED) and (any(
                value is not None
                for value in (title, description, category, impact, weakness_kind)
            ) or (applicability is not None and ApplicabilitySpec.normalize(applicability).model_dump() != row.applicability)
                or (asset_id is not None and asset_id != row.asset_id)
                or (identity_id is not None and identity_id != row.identity_id)):
                raise ApiError(
                    ErrorCode.CONFLICT,
                    "已确认弱点的核心事实不可原地改写；请创建新的候选记录并建立关系。",
                )

        before_hash = row.content_hash or ""
        before_conditions = {str(item.get("key")): item for item in (row.provided_conditions or [])}

        if title is not None:
            row.title = title.strip() or row.title
        if description is not None:
            row.description = description
        if category is not None:
            row.category = str(category)
        if impact is not None:
            row.impact = impact
        if asset_id is not None:
            row.asset_id = asset_id
        if identity_id is not None:
            row.identity_id = identity_id
        if weakness_kind is not None:
            row.weakness_kind = _coerce_kind(weakness_kind)
        if applicability is not None:
            row.applicability = ApplicabilitySpec.normalize(applicability).model_dump()
        row.evidence_refs = _merge(row.evidence_refs, evidence_refs)

        removed = [str(item) for item in (remove_condition_keys or [])]
        if provided_conditions == []:
            removed = list(before_conditions)
        if removed:
            from agent_workbench.persistence.tables import LeadWeaknessLinkRow

            referenced = (await self._s.execute(select(LeadWeaknessLinkRow.id).where(
                LeadWeaknessLinkRow.finding_id == row.id,
                LeadWeaknessLinkRow.condition_key.in_(removed),
            ).limit(1))).scalar_one_or_none()
            lead_rows = (await self._s.execute(select(LeadRow.required_conditions).where(LeadRow.session_id == session_id))).scalars()
            referenced_requirement = any(
                item.get("weakness_id") == str(row.id) and item.get("condition_key") in removed
                for conditions in lead_rows for item in conditions or []
            )
            if referenced is not None or referenced_requirement:
                raise ApiError(ErrorCode.CONFLICT, "条件仍被线索或关系引用，不能删除；请修订条件并保留原 key。")
        for key in removed:
            before_conditions.pop(key, None)

        added: list[str] = []
        revised: list[str] = []
        if provided_conditions is not None:
            if len(provided_conditions) > MAX_CONDITIONS_PER_WRITE:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    f"一次最多写入 {MAX_CONDITIONS_PER_WRITE} 条条件",
                )
            for raw in provided_conditions:
                item = ProvidedCondition.model_validate(_with_condition_key(dict(raw)))
                existing = before_conditions.get(item.key)
                payload = item.model_dump()
                if existing is None:
                    added.append(item.key)
                elif (
                    existing.get("statement") != payload["statement"]
                    or list(existing.get("evidence_refs") or []) != payload["evidence_refs"]
                    or existing.get("limitations") != payload["limitations"]
                ):
                    revised.append(item.key)
                before_conditions[item.key] = payload
        # 稳定顺序，便于"同内容重复更新"与指纹比较。
        row.provided_conditions = sorted(before_conditions.values(), key=lambda x: str(x["key"]))

        after_hash = _content_hash(row)
        if created:
            row.revision = 1
            content_changed = True
        else:
            content_changed = after_hash != before_hash
            if content_changed:
                row.revision = int(row.revision) + 1
        row.content_hash = after_hash

        review_required = False
        review_stale = False
        linked_lead_ids: list[str] = []
        if connection_review is not None:
            review = ConnectionReview.model_validate(connection_review)
            linked_lead_ids = await self._validate_referenced_leads(
                session_id, review.linked_lead_ids, row.id
            )
            if not review.has_judgment():
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    "用途判断必须写明已产生的关联线索，或给出『暂未找到后续用途』的短说明。",
                )
            stored = {
                **review.model_dump(),
                "linked_lead_ids": linked_lead_ids,
                "basis_fingerprint": self._fingerprint(row, linked_lead_ids),
                "reviewed_content_revision": int(row.revision),
            }
            row.connection_review = stored
        else:
            stored = dict(row.connection_review or {})
            linked_lead_ids = [str(item) for item in stored.get("linked_lead_ids") or []]
            review_stale = bool(stored) and stored.get(
                "basis_fingerprint"
            ) != self._fingerprint(row, linked_lead_ids)
            review_required = not stored or review_stale

        await self._s.flush()
        if content_changed and not created:
            # 来源弱点实质变化：下游**已记录**的可推进性判断降级为待复核。
            from agent_workbench.domain.weakness_relations import (
                mark_dependent_judgements_stale,
            )

            await mark_dependent_judgements_stale(self._s, session_id, row.id)
        return WeaknessWriteResult(
            weakness_id=row.id,
            revision=int(row.revision),
            created=created,
            content_changed=content_changed,
            condition_keys=[str(item["key"]) for item in row.provided_conditions],
            added_condition_keys=sorted(added),
            revised_condition_keys=sorted(revised),
            removed_condition_keys=sorted(removed),
            review_required=review_required,
            review_stale=review_stale,
            linked_lead_ids=linked_lead_ids,
        )

    def review_is_current(self, row: FindingRow) -> bool:
        """该弱点的"后续用途判断"是否仍对当前版本有效。"""
        stored = dict(row.connection_review or {})
        if not stored:
            return False
        return stored.get("basis_fingerprint") == self._fingerprint(
            row, [str(item) for item in (stored.get("linked_lead_ids") or [])]
        )

    def _fingerprint(self, row: FindingRow, lead_ids: list[str]) -> str:
        return connection_basis_fingerprint(
            weakness_id=str(row.id),
            content_revision=int(row.revision or 1),
            content_hash=str(row.content_hash or ""),
            lead_ids=list(lead_ids),
            condition_keys=[str(item["key"]) for item in (row.provided_conditions or [])],
        )

    async def _validate_referenced_leads(self, session_id: UUID, raw_ids: list[str], finding_id: UUID) -> list[str]:
        from agent_workbench.persistence.tables import LeadWeaknessLinkRow

        resolved: list[str] = []
        for raw in raw_ids:
            try:
                lead_id = UUID(str(raw))
            except ValueError as exc:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED, f"用途判断引用了非法线索 ID：{raw}"
                ) from exc
            row = (
                await self._s.execute(
                    select(LeadRow.id).where(
                        LeadRow.id == lead_id, LeadRow.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    "『已产生后续线索』的判断必须引用真实存在的关联；"
                    f"线索 {raw} 不在当前会话。",
                )
            linked = (await self._s.execute(select(LeadWeaknessLinkRow.id).where(
                LeadWeaknessLinkRow.session_id == session_id,
                LeadWeaknessLinkRow.lead_id == lead_id,
                LeadWeaknessLinkRow.finding_id == finding_id,
                LeadWeaknessLinkRow.role == "basis",
                LeadWeaknessLinkRow.relation_state != "refuted",
            ).limit(1))).scalar_one_or_none()
            if linked is None:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "用途判断必须引用真实存在的 basis 关系，不能只填写无关线索 ID。")
            resolved.append(str(row))
        return resolved

    async def get(self, finding_id: UUID) -> FindingRow | None:
        return (
            await self._s.execute(select(FindingRow).where(FindingRow.id == finding_id).execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def list_for_session(self, session_id: UUID, *, limit: int = 200) -> list[FindingRow]:
        return list(
            (
                await self._s.execute(
                    select(FindingRow)
                    .where(FindingRow.session_id == session_id)
                    .order_by(FindingRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def confirmed(self, session_id: UUID) -> list[FindingRow]:
        return list(
            (
                await self._s.execute(
                    select(FindingRow).where(
                        FindingRow.session_id == session_id,
                        FindingRow.status == str(FindingStatus.CONFIRMED),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def pending_connection_review(
        self, session_id: UUID, *, limit: int = 50, after_id: UUID | None = None
    ) -> list[FindingRow]:
        """尚未判断后续用途（或判断已过期）的**实质**弱点。

        没有显式关系的新弱点也必须有进入待分析列表的机会，否则无法建立第一条关系。
        `after_id` 是 **id keyset**：按 id 排序推进，避免"按时间排序但按 id 截断"漏行。
        """
        selection = (
            select(FindingRow)
            .where(
                FindingRow.session_id == session_id,
                FindingRow.status != str(FindingStatus.DISPROVED),
            )
            .order_by(FindingRow.id)
        )
        if after_id is not None:
            selection = selection.where(FindingRow.id > after_id)
        rows = list((await self._s.execute(selection.limit(limit))).scalars().all())
        return [row for row in rows if not self.review_is_current(row)]

    async def all_pending_connection_review(
        self, session_id: UUID, *, batch: int = 200
    ) -> list[FindingRow]:
        """**完整**枚举待判断弱点（keyset 分页），不使用固定 limit 当完整集合。"""
        pending: list[FindingRow] = []
        cursor: UUID | None = None
        while True:
            selection = (
                select(FindingRow)
                .where(
                    FindingRow.session_id == session_id,
                    FindingRow.status != str(FindingStatus.DISPROVED),
                )
                .order_by(FindingRow.id)
                .limit(batch)
            )
            if cursor is not None:
                selection = selection.where(FindingRow.id > cursor)
            rows = list((await self._s.execute(selection)).scalars().all())
            if not rows:
                return pending
            pending.extend(row for row in rows if not self.review_is_current(row))
            if len(rows) < batch:
                return pending
            cursor = rows[-1].id


def _coerce_kind(value: str | None) -> str:
    if value is None:
        return str(WeaknessKind.UNCLASSIFIED)
    try:
        return str(WeaknessKind(str(value)))
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, f"weakness_kind 非法：{value}") from exc


def _with_condition_key(raw: dict[str, Any]) -> dict[str, Any]:
    """补齐条件 key：调用方未提供时由陈述派生**稳定** key。

    这样"同一陈述重复写入"会命中同一条条件，不产生知识进展（方案 §3.1）。
    """
    key = str(raw.get("key") or "").strip()
    if key:
        raw["key"] = key
        return raw
    statement = str(raw.get("statement") or "").strip()
    if not statement:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "条件必须提供 statement")
    raw["key"] = canonical_condition_key(statement)
    return raw


def _content_hash(row: FindingRow) -> str:
    core = content_revision_payload(
        title=row.title,
        description=row.description,
        category=row.category,
        weakness_kind=row.weakness_kind,
        applicability=dict(row.applicability or {}),
        provided_conditions=[
            {
                "key": item.get("key"),
                "statement": item.get("statement"),
                "limitations": item.get("limitations"),
                "evidence_refs": item.get("evidence_refs") or [],
            }
            for item in (row.provided_conditions or [])
        ],
    )
    import hashlib
    import json

    return hashlib.sha256(json.dumps({
        "core": core, "evidence_refs": sorted(row.evidence_refs or []),
        "asset_id": str(row.asset_id) if row.asset_id else None,
        "identity_id": str(row.identity_id) if row.identity_id else None,
        "impact": row.impact,
    }, sort_keys=True).encode()).hexdigest()



class VerificationService:
    """独立的确定性核验入口。**模型提出的发现不直接 confirmed**（P6 清单）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        finding_id: UUID,
        method_version: str,
        conclusion: FindingStatus,
        input_evidence_refs: list[str] | None = None,
        recheck_evidence_refs: list[str] | None = None,
        tested_conditions: dict[str, Any] | None = None,
        limitations: str | None = None,
        executed_by_identity_id: UUID | None = None,
        performed_by: str = "code",
    ) -> VerificationRow:
        """写入验证记录并推进发现状态。

        - 非确定性方法（例如"另一个模型也同意"）不允许把状态推进到 confirmed；
        - 阴性结论必须附 `tested_conditions`。
        """
        finding = await FindingService(self._s).get(finding_id)
        if finding is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"发现不存在：{finding_id}")
        from agent_workbench.domain.exploration_lock import lock_exploration

        await lock_exploration(self._s, finding.session_id)
        await self._s.refresh(finding)

        if conclusion is FindingStatus.CONFIRMED and performed_by != "code":
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "只有确定性核验器（performed_by=code）可以确认发现；模型判断不能作为独立验证。",
            )

        if (
            conclusion in {FindingStatus.DISPROVED, FindingStatus.INCONCLUSIVE}
            and not tested_conditions
        ):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "阴性/不确定结论必须记录测试条件（tested_conditions），不能只写结论",
            )

        row = VerificationRow(
            id=uuid4(),
            finding_id=finding_id,
            method_version=method_version,
            input_evidence_refs=list(input_evidence_refs or []),
            recheck_evidence_refs=list(recheck_evidence_refs or []),
            conclusion=str(conclusion),
            limitations=limitations,
            tested_conditions=dict(tested_conditions or {}),
            executed_by_identity_id=executed_by_identity_id,
            performed_by=performed_by,
        )
        self._s.add(row)
        await self._s.flush()

        finding.verification_id = row.id
        finding.status = str(conclusion)
        finding.evidence_refs = _merge(
            finding.evidence_refs,
            list(input_evidence_refs or []) + list(recheck_evidence_refs or []),
        )
        finding.revision = int(finding.revision) + 1
        # 验证结论是实质状态变化：同步刷新内容哈希，使依赖版本的一致性检查仍然成立。
        finding.content_hash = _content_hash(finding)
        await self._s.flush()
        from agent_workbench.domain.weakness_relations import mark_dependent_judgements_stale

        await mark_dependent_judgements_stale(self._s, finding.session_id, finding.id)
        return row

    async def list_for_finding(self, finding_id: UUID) -> list[VerificationRow]:
        return list(
            (
                await self._s.execute(
                    select(VerificationRow)
                    .where(VerificationRow.finding_id == finding_id)
                    .order_by(VerificationRow.created_at)
                )
            )
            .scalars()
            .all()
        )


def profile_definition_hash(profile: CoverageProfile) -> str:
    payload = {
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "categories": list(profile.check_categories),
        "include_parameter_cells": profile.include_parameter_cells,
        "include_anonymous": profile.include_anonymous,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class CoverageService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        session_id: UUID,
        check_category: str,
        status: CoverageStatus,
        asset_id: UUID | None = None,
        entry_label: str = "",
        identity_id: UUID | None = None,
        reason: str | None = None,
        evidence_refs: list[str] | None = None,
        subject_kind: str = "asset",
        parameter_id: UUID | None = None,
        identity_revision: int | None = None,
        condition_fingerprint: str | None = None,
        profile_id: str | None = None,
        profile_version: str | None = None,
        freshness: str = "current",
        resolution_status: str = "resolved",
        evaluation_ref: str | None = None,
    ) -> CoverageRow:
        """按组合键去重写入覆盖记录。

        组合键：`(session_id, asset_id, entry_label, identity_id, check_category)`。
        """
        conditions = [
            CoverageRow.session_id == session_id,
            CoverageRow.asset_id.is_(asset_id)
            if asset_id is None
            else CoverageRow.asset_id == asset_id,
            CoverageRow.entry_label == entry_label,
            CoverageRow.identity_id.is_(identity_id)
            if identity_id is None
            else CoverageRow.identity_id == identity_id,
            CoverageRow.check_category == check_category,
        ]
        if profile_id is not None:
            conditions.extend(
                [
                    CoverageRow.parameter_id.is_(parameter_id)
                    if parameter_id is None
                    else CoverageRow.parameter_id == parameter_id,
                    CoverageRow.identity_revision.is_(identity_revision)
                    if identity_revision is None
                    else CoverageRow.identity_revision == identity_revision,
                    CoverageRow.condition_fingerprint == condition_fingerprint,
                    CoverageRow.profile_id == profile_id,
                    CoverageRow.profile_version == profile_version,
                ]
            )
        existing = (
            await self._s.execute(select(CoverageRow).where(*conditions))
        ).scalar_one_or_none()

        if existing is not None:
            existing.status = str(status)
            existing.reason = reason
            existing.evidence_refs = _merge(existing.evidence_refs, evidence_refs)
            existing.revision = int(existing.revision) + 1
            existing.subject_kind = subject_kind
            existing.parameter_id = parameter_id
            existing.identity_revision = identity_revision
            existing.condition_fingerprint = condition_fingerprint
            existing.profile_id = profile_id
            existing.profile_version = profile_version
            existing.freshness = freshness
            existing.resolution_status = resolution_status
            existing.evaluation_ref = evaluation_ref
            await self._s.flush()
            return existing

        row = CoverageRow(
            id=uuid4(),
            session_id=session_id,
            asset_id=asset_id,
            entry_label=entry_label,
            identity_id=identity_id,
            check_category=check_category,
            status=str(status),
            reason=reason,
            evidence_refs=list(evidence_refs or []),
            subject_kind=subject_kind,
            parameter_id=parameter_id,
            identity_revision=identity_revision,
            condition_fingerprint=condition_fingerprint,
            profile_id=profile_id,
            profile_version=profile_version,
            freshness=freshness,
            resolution_status=resolution_status,
            evaluation_ref=evaluation_ref,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def summary(self, session_id: UUID) -> dict[str, Any]:
        """覆盖摘要。

        **分母明确**时才给比例：分母是"已发现的资产/入口组合"，而不是"全部漏洞类别"。
        """
        rows = list(
            (await self._s.execute(select(CoverageRow).where(CoverageRow.session_id == session_id)))
            .scalars()
            .all()
        )
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1

        checked = by_status.get(str(CoverageStatus.CHECKED_NEGATIVE), 0) + by_status.get(
            str(CoverageStatus.CHECKED_POSITIVE), 0
        )
        denominator = len(rows)
        v2_rows = [row for row in rows if row.profile_id is not None]
        legacy_rows = [row for row in rows if row.profile_id is None]
        applicable = [
            row
            for row in v2_rows
            if not (row.status == str(CoverageStatus.NOT_APPLICABLE) and row.evidence_refs)
        ]
        v2_checked = [
            row
            for row in v2_rows
            if row.status
            in {str(CoverageStatus.CHECKED_NEGATIVE), str(CoverageStatus.CHECKED_POSITIVE)}
            and row.freshness == "current"
            and row.resolution_status == "resolved"
        ]
        return {
            "total_entries": denominator,
            "by_status": by_status,
            "checked_ratio": (round(checked / denominator, 4) if denominator else None),
            "coverage_v2": {
                "total_cells": len(v2_rows),
                "applicable_cells": len(applicable),
                "checked_cells": len(v2_checked),
                "checked_ratio": (
                    round(len(v2_checked) / len(applicable), 4) if applicable else None
                ),
                "legacy_unresolved_cells": sum(
                    1 for row in legacy_rows if row.resolution_status != "resolved"
                ),
                "denominator_definition": (
                    "分母 = 冻结清单版本生成的有限 cell 集合 − 有有效证据的 not_applicable；"
                    "blocked/not_checked/stale 留在分母。"
                ),
            },
            "ratio_definition": (
                "分母 = 已登记的 资产/入口 × 身份 × 检查类别 组合数；"
                "**不是**「网站漏洞覆盖率」，也不能外推到未发现的入口。"
            ),
            "gaps": [
                {
                    "entry_label": row.entry_label,
                    "check_category": row.check_category,
                    "status": row.status,
                    "reason": row.reason,
                }
                for row in rows
                if row.status in {str(CoverageStatus.NOT_CHECKED), str(CoverageStatus.BLOCKED)}
            ],
        }

    async def register_profile(
        self,
        session_id: UUID,
        *,
        profile: CoverageProfile,
        condition_fingerprint: str,
    ) -> CoverageProfileRow:
        """由唯一键原子登记；竞争者重读已提交定义，不修改冻结内容。"""
        definition_hash = profile_definition_hash(profile)
        inserted = (
            await self._s.execute(
                insert(CoverageProfileRow)
                .values(
                    session_id=session_id,
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    condition_fingerprint=condition_fingerprint,
                    definition_hash=definition_hash,
                    categories=list(profile.check_categories),
                    include_parameter_cells=profile.include_parameter_cells,
                    include_anonymous=profile.include_anonymous,
                )
                .on_conflict_do_nothing(constraint="uq_coverage_profile")
                .returning(CoverageProfileRow)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted

        # READ COMMITTED 下，新语句能读取唯一键竞争者刚提交的行。
        # 行锁串行化旧空 hash 的兼容回填；不回滚调用方的整个事务。
        existing = (
            await self._s.execute(
                select(CoverageProfileRow)
                .where(
                    CoverageProfileRow.session_id == session_id,
                    CoverageProfileRow.profile_id == profile.profile_id,
                    CoverageProfileRow.profile_version == profile.profile_version,
                    CoverageProfileRow.condition_fingerprint == condition_fingerprint,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        existing_hash = existing.definition_hash
        if not existing_hash:
            existing_hash = profile_definition_hash(
                CoverageProfile(
                    profile_id=existing.profile_id,
                    profile_version=existing.profile_version,
                    check_categories=tuple(existing.categories or []),
                    include_parameter_cells=bool(existing.include_parameter_cells),
                    include_anonymous=bool(existing.include_anonymous),
                )
            )
        if existing_hash != definition_hash:
            raise ValueError(
                "coverage_profile_conflict: 同一 profile/version/fingerprint "
                "不能携带不同定义"
            )
        if not existing.definition_hash:
            existing.definition_hash = existing_hash
            await self._s.flush()
        return existing

    async def get_profile(
        self,
        session_id: UUID,
        *,
        profile_id: str,
        profile_version: str,
        condition_fingerprint: str,
    ) -> CoverageProfileRow | None:
        return (
            await self._s.execute(
                select(CoverageProfileRow).where(
                    CoverageProfileRow.session_id == session_id,
                    CoverageProfileRow.profile_id == profile_id,
                    CoverageProfileRow.profile_version == profile_version,
                    CoverageProfileRow.condition_fingerprint == condition_fingerprint,
                )
            )
        ).scalar_one_or_none()

    async def registered_profiles(self, session_id: UUID) -> list[CoverageProfileRow]:
        """只读枚举冻结定义；调用方不能用最新一行掩盖多个分母。"""
        return list(
            (
                await self._s.execute(
                    select(CoverageProfileRow)
                    .where(CoverageProfileRow.session_id == session_id)
                    .order_by(
                        CoverageProfileRow.profile_id,
                        CoverageProfileRow.profile_version,
                        CoverageProfileRow.condition_fingerprint,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def latest_profile(
        self, session_id: UUID
    ) -> tuple[CoverageProfile, str] | None:
        row = (
            await self._s.execute(
                select(CoverageProfileRow)
                .where(CoverageProfileRow.session_id == session_id)
                .order_by(CoverageProfileRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return (
            CoverageProfile(
                profile_id=row.profile_id,
                profile_version=row.profile_version,
                check_categories=tuple(row.categories or []),
                include_parameter_cells=bool(row.include_parameter_cells),
                include_anonymous=bool(row.include_anonymous),
            ),
            row.condition_fingerprint,
        )

    async def checklist_summary(
        self,
        session_id: UUID,
        *,
        profile: CoverageProfile,
        condition_fingerprint: str,
        persist_profile: bool = False,
    ) -> dict[str, Any]:
        """用冻结清单生成 Uᵥ，再对账 coverage rows；不是扫描总结展开。"""
        if persist_profile:
            await self.register_profile(
                session_id,
                profile=profile,
                condition_fingerprint=condition_fingerprint,
            )
        session_row = await self._s.get(SessionRow, session_id)
        if session_row is None:
            raise ValueError("session 不存在")
        assets = list(
            (
                await self._s.execute(
                    select(AssetRow).where(
                        AssetRow.session_id == session_id,
                        AssetRow.record_role == "canonical",
                    )
                )
            )
            .scalars()
            .all()
        )
        parameters = list(
            (
                await self._s.execute(
                    select(ParameterRow).where(ParameterRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
        identities = list(
            (
                await self._s.execute(
                    select(IdentityRow).where(IdentityRow.project_id == session_row.project_id)
                )
            )
            .scalars()
            .all()
        )
        cells = build_checklist(
            profile=profile,
            assets=assets,
            parameters=parameters,
            identities=identities,
            condition_fingerprint=condition_fingerprint,
        )
        rows = list(
            (
                await self._s.execute(
                    select(CoverageRow).where(CoverageRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
        return reconcile_checklist(cells, rows)

    async def count_by_status(self, session_id: UUID) -> dict[str, int]:
        rows = (
            await self._s.execute(
                select(CoverageRow.status, func.count())
                .where(CoverageRow.session_id == session_id)
                .group_by(CoverageRow.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}


def _merge(left: list[str] | None, right: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for item in list(left or []) + list(right or []):
        seen.setdefault(str(item), None)
    return list(seen)


def finding_summary(row: FindingRow) -> dict[str, Any]:
    """弱点的有界摘要（旧 API 字段全部保留）。"""
    return {
        "finding_id": str(row.id),
        "weakness_id": str(row.id),
        "title": row.title,
        "category": row.category,
        "status": row.status,
        "weakness_kind": row.weakness_kind or str(WeaknessKind.UNCLASSIFIED),
        "impact": row.impact,
        "evidence_refs": list(row.evidence_refs or []),
        "condition_keys": [str(item.get("key")) for item in (row.provided_conditions or [])],
        "verification_id": str(row.verification_id) if row.verification_id else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(),
    }


def condition_digest(row: FindingRow) -> list[dict[str, Any]]:
    """条件的有界投影：只保留 key、简短陈述与限制，不返回长正文。"""
    return [
        {
            "key": str(item.get("key")),
            "statement": str(item.get("statement") or "")[:400],
            "evidence_refs": list(item.get("evidence_refs") or []),
            "limitations": item.get("limitations"),
        }
        for item in (row.provided_conditions or [])
    ]


def utcnow() -> datetime:
    return datetime.now(UTC)
