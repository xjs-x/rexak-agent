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

from sqlalchemy import select, text
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
        impact: int | None = None,
        evidence_strength: int | None = None,
        feasibility: int | None = None,
        information_gain: int | None = None,
        next_action: str | None = None,
        evidence_refs: list[str] | None = None,
        lead_id: UUID | None = None,
    ) -> LeadRow:
        score, parts = compute_priority(
            impact=impact or 0,
            evidence_strength=evidence_strength or 0,
            feasibility=feasibility or 0,
            information_gain=information_gain or 0,
        )
        scored = any(
            value is not None
            for value in (impact, evidence_strength, feasibility, information_gain)
        )
        if not hypothesis.strip():
            raise ValueError("假设不能为空")
        if lead_id is not None:
            row = await self.get(lead_id)
            if row is None or row.session_id != session_id:
                raise ApiError(ErrorCode.NOT_FOUND, f"线索不存在：{lead_id}")
            row.hypothesis = hypothesis
            row.asset_id = asset_id or row.asset_id
            row.identity_refs = list(identity_refs or row.identity_refs)
            if scored:
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
            priority_rationale=parts if scored else {},
            status=str(LeadStatus.NEW),
            next_action=next_action,
            evidence_refs=list(evidence_refs or []),
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def merge(self, session_id: UUID, source_id: UUID, target_id: UUID) -> LeadRow:
        """同会话串行合并，保留原记录状态；引用并集传播到最终目标。"""
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"lead-merge:{session_id}"},
        )
        source = await self.get(source_id)
        if source is None or source.session_id != session_id:
            raise ValueError("源线索不属于当前会话")
        visited = {source_id}
        cursor = target_id
        while True:
            if cursor in visited:
                raise ValueError("线索合并不能形成循环别名")
            visited.add(cursor)
            target = await self.get(cursor)
            if target is None or target.session_id != session_id:
                raise ValueError("不能跨会话合并线索")
            if target.merged_into_id is None:
                break
            cursor = target.merged_into_id
        if source.merged_into_id is not None and source.merged_into_id != target.id:
            raise ValueError("已合并线索不能重新指向其他目标")
        if source.merged_into_id == target.id:
            refs = _merge(target.evidence_refs, source.evidence_refs)
            identities = _merge(target.identity_refs, source.identity_refs)
            if refs != target.evidence_refs or identities != target.identity_refs:
                target.evidence_refs, target.identity_refs = refs, identities
                target.revision += 1
                await self._s.flush()
            return source
        source.merged_into_id = target.id
        target.evidence_refs = _merge(target.evidence_refs, source.evidence_refs)
        target.identity_refs = _merge(target.identity_refs, source.identity_refs)
        source.revision += 1
        target.revision += 1
        await self._s.flush()
        return source

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
        "merged_into_id": str(row.merged_into_id) if row.merged_into_id else None,
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
