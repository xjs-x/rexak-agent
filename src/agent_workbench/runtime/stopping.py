"""阶段结束的确定性检查（finish_gate）。

V3（方案 §6）：复查集合由**程序**从业务表完整枚举，不再依赖模型在结束申请里主动列出的线索。

决策顺序：
```
if pending_user_commands:                    -> continue
if invalid_evidence_or_unverified_findings:  -> continue（带具体反馈）
if unreviewed_or_stale_items:                -> continue（带回一页复查对象）
if ready_leads_or_feasible_coverage_work:    -> continue
if unresolved_tool_jobs:                     -> wait_tool
if all_remaining_routes_need_external_conditions: -> wait_user
if no_supported_next_action_remains:         -> complete
```

硬性语义：
- `blocked` / `deferred` **不在**自动终态集合中；缺失可推进性判断时归为 `needs_review`；
- `deferred/outside_goal/not_required + reason` 不再直接通过检查；
- "非当前目标必需"不再是自主结束的豁免；
- `no_new_action` 必须绑定本次依赖版本并说明未知，程序检查遗漏、版本与结构矛盾。
- 程序**不能**证明自然语言理由一定正确，也不声称能证明模型想到所有方法。

缺少外部条件（账号、可达目标）时进入 `wait_user`，但未决对象必须保留在复查与报告中，
不能用"把线索改成 blocked/deferred"消除缺口。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import CommandStatus, CoverageStatus
from agent_workbench.contracts.exploration import REVIEW_PAGE_SIZE
from agent_workbench.domain.exploration_review import build_review_inventory
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore, build_artifact_store
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CommandRow,
    CoverageRow,
    FindingRow,
    VerificationRow,
)

Decision = Literal["complete", "continue", "wait_user", "wait_tool"]


@dataclass(slots=True)
class CompletionDecision:
    """至少包含 continue / wait_user / wait_tool / complete，并带理由与待处理对象。"""

    decision: Decision
    reasons: list[str] = field(default_factory=list)
    blocking_objects: dict[str, list[str]] = field(default_factory=dict)
    coverage_gap: dict[str, Any] | None = None
    #: metadata-only 观测（方案 §12）：结束复查的四类计数。
    unreviewed_count: int = 0
    ready_count: int = 0
    blocked_count: int = 0
    #: 复查分页游标（由 checkpoint 保存）；为 None 表示已到末尾，下次从头再核对全量。
    review_next_cursor: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "complete"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "unreviewed_count": self.unreviewed_count,
            "ready_count": self.ready_count,
            "blocked_count": self.blocked_count,
            "reason_count": len(self.reasons),
        }


class FinishGate:
    """在业务表上做确定性检查。**不调用模型。**"""

    def __init__(self, session: AsyncSession, store: ArtifactStore | None = None) -> None:
        self._s = session
        self._store = store or build_artifact_store()

    async def evaluate(
        self,
        *,
        session_id: UUID,
        proposal: Mapping[str, Any] | None,
        plan: Sequence[Mapping[str, Any]] | None = None,
        review_cursor: str | None = None,
    ) -> CompletionDecision:
        from agent_workbench.domain.exploration_lock import lock_exploration

        await lock_exploration(self._s, session_id)
        if proposal is None or proposal.get("reason") not in {
            "goal_answered",
            "exploration_converged",
        }:
            return CompletionDecision(
                decision="continue",
                reasons=["缺少有效的结构化结束提议"],
            )
        if (
            not str(proposal.get("goal_answer") or "").strip()
            or not str(proposal.get("stop_rationale") or "").strip()
        ):
            return CompletionDecision(
                decision="continue",
                reasons=["结束提议必须包含目标回答和停止依据"],
            )

        # 1) 未处理的高优先级用户指令
        pending_command_ids = await self._pending_high_priority_commands(session_id)
        if pending_command_ids:
            return CompletionDecision(
                decision="continue",
                reasons=["存在未处理的高优先级用户指令"],
                blocking_objects={"pending_commands": pending_command_ids},
                ready_count=0,
            )

        # 2) 结束提议引用的证据与已确认发现的可信度
        invalid_refs = await self._invalid_proposal_refs(session_id, proposal)
        unverified = await self._confirmed_without_verification(session_id)
        if invalid_refs or unverified:
            reasons = []
            blocking: dict[str, list[str]] = {}
            if invalid_refs:
                reasons.append("结束提议引用不可用或其他会话证据")
                blocking["invalid_evidence_refs"] = invalid_refs
            if unverified:
                reasons.append("存在缺少验证记录或证据不可用的已确认发现")
                blocking["unverified_findings"] = unverified
            return CompletionDecision(
                decision="continue", reasons=reasons, blocking_objects=blocking
            )

        # 3) 程序枚举的**完整**复查集合（不按 priority 阈值裁剪，也不依赖 .limit(200)）
        inventory = await build_review_inventory(
            self._s,
            session_id,
            plan=plan or [],
            cursor=review_cursor,
            page_size=REVIEW_PAGE_SIZE,
            focus_lead_id=proposal.get("focus_lead_id"),
        )
        page_labels = [
            f"{item.kind}:{item.object_id}:{item.label[:60]}" for item in inventory.items
        ]
        if inventory.unreviewed_count:
            return CompletionDecision(
                decision="continue",
                reasons=[
                    "还有尚未复查或依据已过期的对象（含待分析弱点、失效的条件判断）；"
                    "请先按下面这一页给出判断或推进，再考虑结束。"
                    "`deferred/outside_goal/not_required` 不再是放行理由。",
                ],
                blocking_objects={"review_page": page_labels},
                unreviewed_count=inventory.unreviewed_count,
                ready_count=inventory.actionable_count,
                review_next_cursor=inventory.next_cursor,
            )

        # 4) 仍有可推进方向 -> 继续
        if inventory.actionable_count:
            return CompletionDecision(
                decision="continue",
                reasons=["仍有可推进的线索或覆盖缺口，默认继续而不是结束。"],
                blocking_objects={"actionable_items": page_labels},
                ready_count=inventory.actionable_count,
            )

        # 5) 未交待结果的工具任务
        unresolved_jobs = await self._unresolved_jobs(session_id)
        if unresolved_jobs:
            return CompletionDecision(
                decision="wait_tool",
                reasons=["存在未交待结果的工具任务（含状态未知）"],
                blocking_objects={"unresolved_jobs": unresolved_jobs},
            )

        # 6) 所有有效方向都确实需要外部条件 -> 等待用户（保留全部未决项）
        waiting = [
            item
            for item in inventory.items
            if item.actionability == "needs_condition"
        ]
        external = [str(item) for item in (proposal.get("missing_conditions") or [])]
        if inventory.waiting_count:
            details: list[str] = []
            for item in waiting:
                missing = (item.detail or {}).get("missing_conditions") or []
                details.append(
                    f"{item.object_id}:{item.label[:60]}"
                    + (f"（缺：{'; '.join(str(v) for v in missing[:3])}）" if missing else "")
                )
            return CompletionDecision(
                decision="wait_user",
                reasons=[
                    "剩余有效方向都需要外部条件：",
                    *(f"缺少必要条件：{item}" for item in external),
                ],
                blocking_objects={
                    "waiting_items": details,
                    "missing_conditions": external,
                },
                blocked_count=inventory.waiting_count,
            )

        # 7) 开放探索收尾：覆盖缺口复查（受阻项保留在报告里，不能靠改状态消除）
        if proposal.get("reason") == "exploration_converged":
            gap = await self._coverage_gap(session_id)
            if gap["total_entries"] == 0:
                return CompletionDecision(
                    decision="continue",
                    reasons=["尚未登记任何覆盖组合，不能声称开放探索已经收敛"],
                    coverage_gap=gap,
                )
            if gap.get("not_checked_entries"):
                return CompletionDecision(
                    decision="continue",
                    reasons=[
                        "请求以 exploration_converged 结束，但仍有未检查且未说明原因的覆盖缺口；"
                        "请先补检或给出明确的搁置理由。"
                    ],
                    blocking_objects={"coverage_gaps": gap["not_checked_entries"]},
                    coverage_gap=gap,
                )
            return CompletionDecision(
                decision="complete", reasons=[], coverage_gap=gap
            )

        return CompletionDecision(decision="complete", reasons=[])

    async def _pending_high_priority_commands(self, session_id: UUID) -> list[str]:
        rows = (
            (
                await self._s.execute(
                    select(CommandRow).where(
                        CommandRow.session_id == session_id,
                        CommandRow.status == str(CommandStatus.QUEUED),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [str(row.id) for row in rows]

    async def _unresolved_jobs(self, session_id: UUID) -> list[str]:
        from agent_workbench.persistence.tables import ToolJobRow, ToolOperationRow

        rows = (
            (
                await self._s.execute(
                    select(ToolJobRow)
                    .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .where(
                        ToolOperationRow.session_id == session_id,
                        ToolJobRow.state.in_(["received", "starting", "running", "unknown"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        standalone = (
            (
                await self._s.execute(
                    select(ToolOperationRow).where(
                        ToolOperationRow.session_id == session_id,
                        ToolOperationRow.job_id.is_(None),
                        ToolOperationRow.status.in_(("accepted", "running", "unknown")),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [f"{row.job_id}:{row.state}" for row in rows] + [
            f"operation:{row.operation_id}:{row.status}" for row in standalone
        ]

    async def _invalid_proposal_refs(
        self, session_id: UUID, proposal: Mapping[str, Any]
    ) -> list[str]:
        invalid = []
        for raw in proposal.get("evidence_refs") or []:
            try:
                row = await self._s.get(ArtifactRow, UUID(str(raw)))
                valid = (
                    row is not None
                    and row.session_id == session_id
                    and row.availability == "available"
                    and row.source_operation_id is not None
                    and not row.truncated
                    and self._store.verify(row.relative_key, row.sha256)
                )
            except (ValueError, ArtifactError):
                valid = False
            if not valid:
                invalid.append(str(raw))
        return invalid

    async def _confirmed_without_verification(self, session_id: UUID) -> list[str]:
        rows = (
            (
                await self._s.execute(
                    select(FindingRow).where(
                        FindingRow.session_id == session_id,
                        FindingRow.status == "confirmed",
                    )
                )
            )
            .scalars()
            .all()
        )
        problems: list[str] = []
        for row in rows:
            if row.verification_id is None:
                problems.append(f"{row.id}:缺少 verification 记录")
                continue
            verification = (
                await self._s.execute(
                    select(VerificationRow).where(
                        VerificationRow.id == row.verification_id,
                        VerificationRow.finding_id == row.id,
                        VerificationRow.conclusion == "confirmed",
                        VerificationRow.performed_by == "code",
                    )
                )
            ).scalar_one_or_none()
            if verification is None:
                problems.append(f"{row.id}:独立验证记录不匹配")
                continue
            if not row.evidence_refs:
                problems.append(f"{row.id}:没有可用证据引用")
                continue
            for ref in row.evidence_refs:
                try:
                    artifact_id = UUID(ref)
                except ValueError:
                    problems.append(f"{row.id}:证据引用不是工件 ID")
                    continue
                artifact = (
                    await self._s.execute(
                        select(ArtifactRow).where(
                            ArtifactRow.id == artifact_id,
                            ArtifactRow.session_id == session_id,
                            ArtifactRow.availability == "available",
                            ArtifactRow.truncated.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                if artifact is None:
                    problems.append(f"{row.id}:证据 {artifact_id} 不可用")
                    continue
                try:
                    valid = self._store.verify(artifact.relative_key, artifact.sha256)
                except ArtifactError:
                    valid = False
                if not valid:
                    problems.append(f"{row.id}:证据 {artifact_id} 文件缺失或哈希不符")
        return problems

    async def _coverage_gap(self, session_id: UUID) -> dict[str, Any]:
        rows = (
            (await self._s.execute(select(CoverageRow).where(CoverageRow.session_id == session_id)))
            .scalars()
            .all()
        )
        not_checked = [
            f"{row.entry_label or row.asset_id}:{row.check_category}"
            for row in rows
            if row.status == str(CoverageStatus.NOT_CHECKED)
        ]
        blocked = [
            f"{row.entry_label or row.asset_id}:{row.check_category}({row.reason or '未说明'})"
            for row in rows
            if row.status == str(CoverageStatus.BLOCKED)
        ]
        return {
            "total_entries": len(rows),
            "not_checked_entries": not_checked,
            "blocked_entries": blocked,
            "note": ("分母是已登记的 资产/入口 × 身份 × 检查类别 组合数，不是「网站漏洞覆盖率」。"),
        }


def no_progress_signature(state: Mapping[str, Any]) -> str:
    """计算"可能算进展"的签名。

    基于新增可用证据、有效入口、假设变化、验证改善；
    **相同结果再写一遍不算进展**（§6.5）。
    """
    evidence = sorted(str(item) for item in (state.get("evidence_refs") or []))
    leads = sorted(
        f"{item.get('lead_id')}:{item.get('status')}" for item in (state.get("plan") or [])
    )
    coverage = state.get("coverage_revision")
    return "|".join([",".join(evidence[-20:]), ",".join(leads[-20:]), str(coverage)])
