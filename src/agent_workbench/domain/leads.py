"""线索（假设）与其优先级。

状态：`new / investigating / needs_verification / confirmed / disproved / blocked / deferred`。

硬约束：
- `blocked` / `deferred` **必须**有理由与恢复条件（§6.5）——数据库 CHECK 强制
  `blocked_reason NOT NULL`；
- 优先级考虑**影响、已有证据、可执行性、预期信息增益**，不只按扫描器严重等级（P6 清单）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import LEAD_STATUSES_REQUIRE_REASON, ErrorCode, LeadStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.tables import LeadRow


def compute_priority(
    *,
    impact: int,
    evidence_strength: int,
    feasibility: int,
    information_gain: int,
) -> tuple[int, dict[str, int]]:
    """加权优先级。四个维度均为 0-5 整数，返回 0-100 的分数与分项。

    权重是**可解释的初值**，不是"通用最佳实践"：
    影响与信息增益更重，因为目标是"尽快获得有效结论"而不是"覆盖最多告警"。
    """
    weights = {
        "impact": 3,
        "evidence_strength": 2,
        "feasibility": 2,
        "information_gain": 3,
    }
    raw = {
        "impact": max(0, min(5, impact)),
        "evidence_strength": max(0, min(5, evidence_strength)),
        "feasibility": max(0, min(5, feasibility)),
        "information_gain": max(0, min(5, information_gain)),
    }
    total_weight = sum(weights.values())
    score = sum(raw[key] * weights[key] for key in weights) * 100 // (5 * total_weight)
    return score, raw


class LeadService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self,
        *,
        session_id: UUID,
        hypothesis: str,
        asset_id: UUID | None = None,
        identity_refs: list[str] | None = None,
        impact: int = 3,
        evidence_strength: int = 1,
        feasibility: int = 3,
        information_gain: int = 3,
        next_action: str | None = None,
        evidence_refs: list[str] | None = None,
        lead_id: UUID | None = None,
    ) -> LeadRow:
        score, parts = compute_priority(
            impact=impact,
            evidence_strength=evidence_strength,
            feasibility=feasibility,
            information_gain=information_gain,
        )
        if lead_id is not None:
            row = await self.get(lead_id)
            if row is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"线索不存在：{lead_id}")
            row.hypothesis = hypothesis
            row.asset_id = asset_id or row.asset_id
            row.identity_refs = list(identity_refs or row.identity_refs)
            row.priority = score
            row.priority_rationale = parts
            row.next_action = next_action or row.next_action
            row.evidence_refs = _merge(row.evidence_refs, evidence_refs)
            row.revision = int(row.revision) + 1
            await self._s.flush()
            return row

        row = LeadRow(
            id=uuid4(),
            session_id=session_id,
            hypothesis=hypothesis,
            asset_id=asset_id,
            identity_refs=list(identity_refs or []),
            priority=score,
            priority_rationale=parts,
            status=str(LeadStatus.NEW),
            next_action=next_action,
            evidence_refs=list(evidence_refs or []),
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, lead_id: UUID) -> LeadRow | None:
        return (
            await self._s.execute(select(LeadRow).where(LeadRow.id == lead_id))
        ).scalar_one_or_none()

    async def set_status(
        self,
        lead_id: UUID,
        status: LeadStatus,
        *,
        reason: str | None = None,
        resume_condition: str | None = None,
        next_action: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> LeadRow:
        row = await self.get(lead_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"线索不存在：{lead_id}")

        if status in LEAD_STATUSES_REQUIRE_REASON and not (reason or row.blocked_reason):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"线索状态 {status} 必须提供理由（bound 条件）与恢复条件",
                details={"lead_id": str(lead_id)},
            )
        if status is LeadStatus.DISPROVED and not reason:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "标记 disproved 必须写清**排除条件**，不能只写'不存在'",
            )

        row.status = str(status)
        if reason is not None:
            row.blocked_reason = reason
        if resume_condition is not None:
            row.resume_condition = resume_condition
        if next_action is not None:
            row.next_action = next_action
        row.evidence_refs = _merge(row.evidence_refs, evidence_refs)
        row.revision = int(row.revision) + 1
        await self._s.flush()
        return row

    async def list_for_session(self, session_id: UUID, *, limit: int = 200) -> list[LeadRow]:
        return list(
            (
                await self._s.execute(
                    select(LeadRow)
                    .where(LeadRow.session_id == session_id)
                    .order_by(LeadRow.priority.desc(), LeadRow.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def open_high_priority(self, session_id: UUID, *, threshold: int = 60) -> list[LeadRow]:
        """finish_gate 检查项 3：重点线索必须已验证或有明确阻碍。"""
        rows = await self.list_for_session(session_id)
        terminal = {
            str(LeadStatus.CONFIRMED),
            str(LeadStatus.DISPROVED),
            str(LeadStatus.BLOCKED),
            str(LeadStatus.DEFERRED),
        }
        return [
            row for row in rows if int(row.priority) >= threshold and row.status not in terminal
        ]


def _merge(left: list[str] | None, right: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for item in list(left or []) + list(right or []):
        seen.setdefault(str(item), None)
    return list(seen)


def lead_summary(row: LeadRow) -> dict[str, Any]:
    return {
        "lead_id": str(row.id),
        "hypothesis": row.hypothesis,
        "status": row.status,
        "priority": row.priority,
        "priority_rationale": row.priority_rationale,
        "next_action": row.next_action,
        "blocked_reason": row.blocked_reason,
        "resume_condition": row.resume_condition,
        "evidence_refs": list(row.evidence_refs or []),
        "revision": row.revision,
    }
