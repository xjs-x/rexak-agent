"""发现、验证与覆盖。

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
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import (
    CoverageStatus,
    ErrorCode,
    FindingCategory,
    FindingStatus,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.tables import (
    CoverageRow,
    FindingRow,
    VerificationRow,
)


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
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, finding_id: UUID) -> FindingRow | None:
        return (
            await self._s.execute(select(FindingRow).where(FindingRow.id == finding_id))
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
        await self._s.flush()
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
    ) -> CoverageRow:
        """按组合键去重写入覆盖记录。

        组合键：`(session_id, asset_id, entry_label, identity_id, check_category)`。
        """
        existing = (
            await self._s.execute(
                select(CoverageRow).where(
                    CoverageRow.session_id == session_id,
                    CoverageRow.asset_id.is_(asset_id)
                    if asset_id is None
                    else CoverageRow.asset_id == asset_id,
                    CoverageRow.entry_label == entry_label,
                    CoverageRow.identity_id.is_(identity_id)
                    if identity_id is None
                    else CoverageRow.identity_id == identity_id,
                    CoverageRow.check_category == check_category,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.status = str(status)
            existing.reason = reason
            existing.evidence_refs = _merge(existing.evidence_refs, evidence_refs)
            existing.revision = int(existing.revision) + 1
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
        return {
            "total_entries": denominator,
            "by_status": by_status,
            "checked_ratio": (round(checked / denominator, 4) if denominator else None),
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
    return {
        "finding_id": str(row.id),
        "title": row.title,
        "category": row.category,
        "status": row.status,
        "impact": row.impact,
        "evidence_refs": list(row.evidence_refs or []),
        "verification_id": str(row.verification_id) if row.verification_id else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(),
    }


def utcnow() -> datetime:
    return datetime.now(UTC)
