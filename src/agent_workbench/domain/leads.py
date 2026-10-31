"""线索（假设）与其优先级。

状态：`new / investigating / needs_verification / confirmed / disproved / blocked / deferred`。

硬约束：
- `blocked` / `deferred` **必须**有理由与恢复条件（§6.5）——数据库 CHECK 强制
  `blocked_reason NOT NULL`；
- 优先级考虑**影响、已有证据、可执行性、预期信息增益**，不只按扫描器严重等级（P6 清单）。

V3 扩展（方案 §3.2）：线索额外承载**待验证连接**——
- `required_conditions`：条件 key/描述，以及可选的已有对象引用或预期产出该条件的 lead_id
  （引用另一条线索的预期结果只表示**依赖意图**，不伪造一个已经存在的弱点）；
- `actionability_assessment`：当前能否推进（`ready/needs_condition/no_new_action/needs_review`），
  与 `status`（工作状态）分开。`deferred + ready` 表示"可以做但排在后面"，仍然阻止自主结束；
  `blocked` 但没有明确缺失条件时归为 `needs_review`，不能直接放行。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import (
    LEAD_STATUSES_REQUIRE_REASON,
    Actionability,
    ErrorCode,
    LeadStatus,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.weaknesses import (
    RequiredConditionRef,
    actionability_basis_fingerprint,
)
from agent_workbench.domain.exploration_lock import lock_exploration
from agent_workbench.persistence.tables import (
    ArtifactRow,
    AssetRow,
    FindingRow,
    IdentityRow,
    LeadRow,
    SessionRow,
    ToolOperationRow,
)


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
        required_conditions: list[dict[str, Any]] | None = None,
        expected_revision: int | None = None,
    ) -> LeadRow:
        await lock_exploration(self._s, session_id)
        if required_conditions is not None:
            await self._validate_conditions(session_id, required_conditions)
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
            visited = {row.id}
            while row.merged_into_id is not None:
                target = await self.get(row.merged_into_id)
                if target is None or target.session_id != session_id or target.id in visited:
                    raise ApiError(ErrorCode.CONFLICT, "线索别名关系不可解析")
                visited.add(target.id)
                row = target
            if expected_revision is not None and int(row.revision) != int(expected_revision):
                raise ApiError(
                    ErrorCode.CONFLICT,
                    f"线索 {lead_id} 已被更新到第 {row.revision} 版，"
                    f"本次基于第 {expected_revision} 版的写入已过期；请重新读取后再提交。",
                    details={"current_revision": row.revision},
                )
            row.hypothesis = hypothesis
            row.asset_id = asset_id or row.asset_id
            row.identity_refs = list(identity_refs or row.identity_refs)
            if scored:
                row.priority = score
                row.priority_rationale = parts
            if next_action is not None:
                row.next_action = next_action
            row.evidence_refs = _merge(row.evidence_refs, evidence_refs)
            if required_conditions is not None:
                row.required_conditions = normalize_conditions(required_conditions)
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
            required_conditions=normalize_conditions(required_conditions),
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def merge(self, session_id: UUID, source_id: UUID, target_id: UUID) -> LeadRow:
        """同会话串行合并，保留原记录状态；引用并集传播到最终目标。"""
        await lock_exploration(self._s, session_id)
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
            conditions = _merge_conditions(target.required_conditions, source.required_conditions)
            if (
                refs != target.evidence_refs
                or identities != target.identity_refs
                or conditions != list(target.required_conditions or [])
            ):
                target.evidence_refs, target.identity_refs = refs, identities
                target.required_conditions = conditions
                target.revision += 1
                await self._s.flush()
            from agent_workbench.domain.weakness_relations import repoint_links

            await repoint_links(self._s, session_id, source.id, target.id)
            return source
        source.merged_into_id = target.id
        target.evidence_refs = _merge(target.evidence_refs, source.evidence_refs)
        target.identity_refs = _merge(target.identity_refs, source.identity_refs)
        target.required_conditions = _merge_conditions(
            target.required_conditions, source.required_conditions
        )
        source.revision += 1
        target.revision += 1
        await self._s.flush()
        # 关系改挂到规范主线索并去重；否则关系会挂在已失效的别名上。
        from agent_workbench.domain.weakness_relations import repoint_links

        await repoint_links(self._s, session_id, source.id, target.id)
        # 合并改变了目标线索的前置条件与关系，旧的可推进性判断随之过期。
        await self._invalidate_actionability(target)
        return source

    async def assess_actionability(
        self,
        lead_id: UUID,
        *,
        state: str,
        note: str,
        evidence_refs: list[str] | None = None,
        operation_refs: list[str] | None = None,
        unknowns: list[str] | None = None,
        source_tool_call_id: str | None = None,
    ) -> LeadRow:
        """写入**当前能否推进**的判断。

        `no_new_action` 必须绑定本次依赖版本并说明尚有哪些未知；
        仍保留明确下一动作或来源已更新时，程序拒绝据它结束（在结束判定处检查）。
        """
        try:
            parsed = Actionability(state)
        except ValueError as exc:
            raise ApiError(ErrorCode.VALIDATION_FAILED, f"actionability 非法：{state}") from exc
        row = await self.get(lead_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"线索不存在：{lead_id}")
        await lock_exploration(self._s, row.session_id)
        await self._s.refresh(row)
        for raw in evidence_refs or []:
            artifact = await self._s.get(ArtifactRow, UUID(raw))
            if artifact is None or artifact.session_id != row.session_id or artifact.availability != "available" or artifact.source_operation_id is None:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "判断证据必须来自当前会话真实工具工件")
        for raw in operation_refs or []:
            operation = await self._s.get(ToolOperationRow, UUID(raw))
            if operation is None or operation.session_id != row.session_id or operation.status not in {"succeeded", "failed", "cancelled"} or operation.tool_name in {"record_lead", "record_weakness", "propose_finding", "update_plan", "propose_completion"}:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "判断操作依据必须是当前会话已有结果的真实尝试")
        if parsed is Actionability.NO_NEW_ACTION and not (unknowns or []):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "no_new_action 必须说明尚有哪些未知、以及为什么当前没有能区分它们的新动作"
                "（unknowns），不能只用文字声明『没有新动作』。",
            )
        if parsed is Actionability.READY and not (row.next_action or "").strip():
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "ready 必须写明具体的下一步（next_action），否则不能声称可以推进。",
            )
        fingerprint = await self.actionability_fingerprint(row)
        stored = {
            "state": str(parsed),
            "note": str(note).strip()[:500],
            "basis_fingerprint": fingerprint,
            "evidence_refs": list(evidence_refs or []),
            "operation_refs": list(operation_refs or []),
            "unknowns": [str(item) for item in (unknowns or [])],
            "source_tool_call_id": source_tool_call_id,
            "revision": int((row.actionability_assessment or {}).get("revision") or 0) + 1,
        }
        if not stored["note"]:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "可推进性判断必须附短说明")
        row.actionability_assessment = stored
        await self._s.flush()
        return row

    async def actionability_fingerprint(self, row: LeadRow) -> str:
        """依据指纹：覆盖线索假设、下一动作、条件、来源弱点的有效版本与身份状态。"""
        from agent_workbench.domain.weakness_relations import links_for_lead

        links = await links_for_lead(self._s, row.session_id, row.id)
        revisions: list[tuple[str, int | None]] = []
        for link in links:
            finding = await self._s.get(FindingRow, link.finding_id)
            revisions.append((str(link.finding_id), int(finding.revision) if finding else None))
            revisions.append((f"link:{link.id}:{link.role}:{link.relation_state}:{link.source_weakness_revision}", int(link.revision)))
        owner = await self._s.get(SessionRow, row.session_id, populate_existing=True)
        identity_ids = set(row.identity_refs or [])
        if owner and owner.active_identity_id:
            identity_ids.add(str(owner.active_identity_id))
        conditions = list(row.required_conditions or [])
        for condition in conditions:
            if condition.get("identity_id"):
                identity_ids.add(str(condition["identity_id"]))
            for field, model in (("weakness_id", FindingRow), ("producer_lead_id", LeadRow)):
                if condition.get(field):
                    dependency = await self._s.get(model, UUID(condition[field]))
                    revisions.append((f"{field}:{condition[field]}:{getattr(dependency, 'status', None)}", int(dependency.revision) if dependency else None))
        identities = []
        for raw in sorted(identity_ids):
            try:
                identity = await self._s.get(IdentityRow, UUID(raw), populate_existing=True)
            except ValueError:
                identity = None  # 旧身份标签仍保留，但不伪造身份实体。
            identities.append((raw, identity.auth_revision if identity else None))
        environment = hashlib.sha256(json.dumps({
            "identities": identities,
            "active": str(owner.active_identity_id) if owner else None,
            "scope": owner.scope_snapshot if owner else {},
            "asset_id": str(row.asset_id) if row.asset_id else None,
        }, sort_keys=True).encode()).hexdigest()
        return actionability_basis_fingerprint(
            hypothesis=row.hypothesis,
            next_action=row.next_action,
            required_conditions=list(row.required_conditions or []),
            basis_revisions=revisions,
            identity_state=environment,
        )

    async def _validate_conditions(self, session_id: UUID, conditions: list[dict[str, Any]]) -> None:
        owner = await self._s.get(SessionRow, session_id)
        for condition in conditions:
            for field, model in (("weakness_id", FindingRow), ("producer_lead_id", LeadRow), ("asset_id", AssetRow), ("identity_id", IdentityRow)):
                if not condition.get(field):
                    continue
                try:
                    dependency = await self._s.get(model, UUID(condition[field]))
                except ValueError as exc:
                    raise ApiError(ErrorCode.VALIDATION_FAILED, f"条件引用 {field} 必须为对象 ID") from exc
                valid = dependency is not None and (
                    (dependency.session_id == owner.id or dependency.id == owner.active_identity_id) if isinstance(dependency, IdentityRow) and owner else getattr(dependency, "session_id", None) == session_id
                )
                if not valid:
                    raise ApiError(ErrorCode.VALIDATION_FAILED, f"条件引用 {field} 不属于当前会话/项目")

    async def is_actionability_stale(self, row: LeadRow) -> bool:
        stored = dict(row.actionability_assessment or {})
        if not stored:
            return True
        return stored.get("basis_fingerprint") != await self.actionability_fingerprint(row)

    async def _invalidate_actionability(self, row: LeadRow) -> None:
        """条件/来源变化后，旧判断成为 `needs_review`，**不**直接判成可执行或已解决。"""
        stored = dict(row.actionability_assessment or {})
        if not stored:
            return
        if stored.get("basis_fingerprint") == await self.actionability_fingerprint(row):
            return
        row.actionability_assessment = {
            **stored,
            "state": str(Actionability.NEEDS_REVIEW),
            "basis_fingerprint": await self.actionability_fingerprint(row),
            "revision": int(stored.get("revision") or 0) + 1,
        }
        await self._s.flush()

    async def update_conditions(
        self, lead_id: UUID, conditions: list[dict[str, Any]]
    ) -> LeadRow:
        row = await self.get(lead_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"线索不存在：{lead_id}")
        await lock_exploration(self._s, row.session_id)
        await self._validate_conditions(row.session_id, conditions)
        before = list(row.required_conditions or [])
        row.required_conditions = normalize_conditions(conditions)
        if row.required_conditions != before:
            row.revision = int(row.revision) + 1
        await self._s.flush()
        await self._invalidate_actionability(row)
        return row

    async def get(self, lead_id: UUID) -> LeadRow | None:
        return (
            await self._s.execute(select(LeadRow).where(LeadRow.id == lead_id).execution_options(populate_existing=True))
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

        await lock_exploration(self._s, row.session_id)
        await self._s.refresh(row)

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

    async def iter_for_session(self, session_id: UUID, *, batch: int = 200) -> list[LeadRow]:
        """**完整**枚举会话内线索（keyset 分页，不使用 `.limit(200)` 当完整集合）。

        结束复查集合必须由程序枚举全部对象；分页只影响每次交回模型的**页**。
        """
        rows: list[LeadRow] = []
        cursor: UUID | None = None
        while True:
            selection = (
                select(LeadRow)
                .where(LeadRow.session_id == session_id)
                .order_by(LeadRow.id)
                .limit(batch)
            )
            if cursor is not None:
                selection = selection.where(LeadRow.id > cursor)
            batch_rows = list((await self._s.execute(selection)).scalars().all())
            if not batch_rows:
                return rows
            rows.extend(batch_rows)
            cursor = batch_rows[-1].id

    async def open_high_priority(self, session_id: UUID, *, threshold: int = 60) -> list[LeadRow]:
        """仅供展示/诊断参考的辅助方法。

        **不是**结束门禁的真源：结束复查集合由 `domain/exploration_review` 完整枚举。
        """
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


def normalize_conditions(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """规范化前置条件；未提供 key 时由描述派生稳定 key。"""
    out: dict[str, dict[str, Any]] = {}
    for item in raw or []:
        payload = dict(item)
        key = str(payload.get("condition_key") or "").strip()
        if not key:
            from agent_workbench.contracts.weaknesses import canonical_condition_key

            key = canonical_condition_key(str(payload.get("description") or ""), seed="requirement")
        payload["condition_key"] = key
        ref = RequiredConditionRef.model_validate({**payload, "condition_key": key})
        out[key] = ref.model_dump()
    return sorted(out.values(), key=lambda item: str(item["condition_key"]))


def _merge_conditions(
    left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in list(left or []) + list(right or []):
        key = str(item.get("condition_key") or "")
        if key in merged and merged[key] != item:
            raise ApiError(ErrorCode.CONFLICT, "同名条件的身份、来源或成立状态不同，不能合并丢弃；请分别保留线索并共用计划步骤。")
        if key and key not in merged:
            merged[key] = dict(item)
    return sorted(merged.values(), key=lambda item: str(item["condition_key"]))


def stored_actionability(row: LeadRow) -> dict[str, Any]:
    return dict(row.actionability_assessment or {})


def lead_summary(row: LeadRow) -> dict[str, Any]:
    assessment = stored_actionability(row)
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
        "required_conditions": list(row.required_conditions or []),
        "actionability": assessment.get("state"),
        "actionability_note": assessment.get("note"),
        "evidence_refs": list(row.evidence_refs or []),
        "revision": row.revision,
    }


def _merge(left: list[str] | None, right: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for item in list(left or []) + list(right or []):
        seen.setdefault(str(item), None)
    return list(seen)


__all__ = [
    "LeadService",
    "compute_priority",
    "lead_summary",
    "normalize_conditions",
    "stored_actionability",
]
