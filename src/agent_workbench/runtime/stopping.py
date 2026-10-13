"""阶段结束的确定性检查（finish_gate）。

检查项（§6.5，**全部可确定性判定**，不额外增加评审模型调用）：

1. 没有未处理的**高优先级用户指令**；
2. 没有未交待的**运行中 job** 或执行状态未知的关键操作；
3. 提议声明的目标相关未决线索有处理结果或具体搁置理由，不按固定分数强制收尾；
4. 确认发现具有**验证记录与可用证据**；
5. 开放探索收尾时做一次**覆盖缺口复查**，仍无有效方向才允许 `exploration_converged`；
6. 缺账号/目标不可达等问题进入 `waiting_user`/受阻说明，不冒充完成。

"无进展"判断基于新增可用证据、有效入口、假设变化、验证改善；
**相同结果再写一遍不算进展**。重复出现时先检查新条件、切换路线，
只有全部可继续路线收敛才结束阶段。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import CommandStatus, CoverageStatus, LeadStatus
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore, build_artifact_store
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CommandRow,
    CoverageRow,
    FindingRow,
    LeadRow,
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

    @property
    def allowed(self) -> bool:
        return self.decision == "complete"


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
    ) -> CompletionDecision:
        reasons: list[str] = []
        blocking: dict[str, list[str]] = {}

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
            reasons.append("存在未处理的高优先级用户指令")
            blocking["pending_commands"] = pending_command_ids

        # 2) 未交待的运行中 job / 状态未知的关键操作
        unresolved_jobs = await self._unresolved_jobs(session_id)
        if unresolved_jobs:
            reasons.append("存在未交待结果的工具任务（含状态未知）")
            blocking["unresolved_jobs"] = unresolved_jobs

        # 3) 重点线索必须已验证或有明确阻碍
        open_leads = await self._open_relevant_leads(session_id, proposal)
        if open_leads:
            reasons.append("目标相关未决线索缺少处理或搁置理由")
            blocking["open_leads"] = open_leads

        # 4) 确认发现必须有验证记录且证据可用
        unverified = await self._confirmed_without_verification(session_id)
        invalid_refs = await self._invalid_proposal_refs(session_id, proposal)
        if invalid_refs:
            reasons.append("结束提议引用不可用或其他会话证据")
            blocking["invalid_evidence_refs"] = invalid_refs
        if unverified:
            reasons.append("存在缺少验证记录或证据不可用的已确认发现")
            blocking["unverified_findings"] = unverified

        # 缺账号/连通性等外部前置条件时，重点线索可能仍是 open；应明确等待用户，
        # 但未决任务、待处理指令和不可信的确认发现不能借此被掩盖。
        missing_conditions = proposal.get("missing_conditions") or []
        if missing_conditions and not (
            pending_command_ids or unresolved_jobs or unverified or invalid_refs
        ):
            return CompletionDecision(
                decision="wait_user",
                reasons=[*reasons, *(f"缺少必要条件：{item}" for item in missing_conditions)],
                blocking_objects={
                    **blocking,
                    "missing_conditions": [str(item) for item in missing_conditions],
                },
            )

        if reasons:
            return CompletionDecision(
                decision="wait_tool"
                if unresolved_jobs
                and not (pending_command_ids or unverified or invalid_refs or open_leads)
                else "continue",
                reasons=reasons,
                blocking_objects=blocking,
            )

        # 5) 开放探索：收尾覆盖缺口复查
        if proposal is not None and proposal.get("reason") == "exploration_converged":
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
                        "请先补检或给出明确的搁置理由（不要直接把线索改成 deferred 绕过检查）。"
                    ],
                    blocking_objects={"coverage_gaps": gap["not_checked_entries"]},
                    coverage_gap=gap,
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

    async def _open_relevant_leads(
        self, session_id: UUID, proposal: Mapping[str, Any]
    ) -> list[str]:
        declared = (proposal.get("coverage_summary") or {}).get("unresolved_items") or []
        explanations = {
            str(item.get("lead_id")): item for item in declared if isinstance(item, dict)
        }
        ids = set(proposal.get("relevant_lead_ids") or []) | set(explanations)
        for raw in proposal.get("remaining_leads") or []:
            try:
                ids.add(str(UUID(str(raw))))
            except ValueError:
                continue  # 旧工具允许文字说明，保留为报告资料，不伪造 ID。
        parsed = []
        for raw in ids:
            try:
                parsed.append(UUID(str(raw)))
            except ValueError:
                return [f"{raw}:目标相关线索 ID 无效"]
        if not parsed:
            return []
        rows = (
            (
                await self._s.execute(
                    select(LeadRow).where(
                        LeadRow.session_id == session_id,
                        LeadRow.id.in_(parsed),
                    )
                )
            )
            .scalars()
            .all()
        )
        terminal = {
            str(LeadStatus.CONFIRMED),
            str(LeadStatus.DISPROVED),
            str(LeadStatus.BLOCKED),
        }
        problems = [
            f"{key}:不属于当前会话" for key in parsed if key not in {row.id for row in rows}
        ]
        for row in rows:
            if row.status in terminal or row.merged_into_id is not None:
                continue
            explanation = explanations.get(str(row.id)) or {}
            if (
                explanation.get("disposition") in {"deferred", "outside_goal", "not_required"}
                and str(explanation.get("reason") or "").strip()
            ):
                continue
            problems.append(f"{row.id}:{row.status}:{row.hypothesis[:60]}")
        return problems

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
