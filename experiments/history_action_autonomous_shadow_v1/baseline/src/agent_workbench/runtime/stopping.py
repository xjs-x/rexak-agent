# 历史离线兼容服务：生产 Agent/Runner 不再调用本模块作结束审查。
"""退役结束协议的离线兼容服务；生产 Agent/Runner 不再调用。

保留历史工具、报告与证据校验测试的旧合同，不用于判断新运行能否结束。
自然结束与执行状态收尾由 Runner 沿官方图边界处理。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import CommandStatus, CoverageStatus
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
    #: **保留资源**（阶段结束仍存活、不等于未完成工作）：终端/监视器/后台任务。
    retained_resources: list[str] = field(default_factory=list)
    #: 持续值班状态（有效 continuous 委托）。报告必须显示"持续值班中"。
    duty_note: str = ""

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
            "retained_resources": list(self.retained_resources),
            "duty_note": self.duty_note,
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
        run_id: UUID | None = None,
    ) -> CompletionDecision:
        # 计划、假设和覆盖记录是可选上下文，不参与结束决策。
        _ = plan, review_cursor, run_id
        if proposal is None or proposal.get("reason") not in {
            "goal_answered",
            "exploration_converged",
        }:
            return CompletionDecision(decision="continue", reasons=["缺少有效结束信号"])
        if not str(proposal.get("goal_answer") or "").strip():
            return CompletionDecision(decision="continue", reasons=["最终回答不能为空"])

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

        # 5) 未交待结果的工具任务与未满足的明确等待条件
        unresolved_jobs = await self._unresolved_jobs(session_id)
        open_wait_sets = await self._open_wait_sets(session_id)
        if unresolved_jobs or open_wait_sets:
            return CompletionDecision(
                decision="wait_tool",
                reasons=["存在未交待结果的工具任务或未满足的等待条件（含状态未知）"],
                blocking_objects={
                    "unresolved_jobs": unresolved_jobs,
                    "open_wait_sets": open_wait_sets,
                },
            )

        # 5b) 保留资源与值班状态：**报告**它们，但不把它们当成"未完成工作"，
        #     也不允许把真正待办（上面的有限任务）改称"保留资源"来绕过检查。
        retained, duty_note = await self._retained_resources(session_id)

        # 缺少条件由模型在结果中说明，不从领域记录推导强制继续或等待。
        return CompletionDecision(
            decision="complete", reasons=[], retained_resources=retained, duty_note=duty_note
        )

    async def _open_wait_sets(self, session_id: UUID) -> list[str]:
        from agent_workbench.persistence.tables import ExecutionWaitSetRow

        rows = (
            (
                await self._s.execute(
                    select(ExecutionWaitSetRow).where(
                        ExecutionWaitSetRow.session_id == session_id,
                        ExecutionWaitSetRow.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )
        return [f"{row.id}（match={row.match}）" for row in rows]

    async def _retained_resources(self, session_id: UUID) -> tuple[list[str], str]:
        """阶段结束仍存活的资源与值班状态。

        - 终端/监视器本身**不隐式阻塞**完成，但必须出现在报告里；
        - 有效持续委托 -> "持续值班中"；无委托则如实说明"保留但不值班"。
        """
        from agent_workbench.persistence.tables import (
            ExecutionDelegationRow,
            ExecutionResourceRow,
        )

        rows = (
            (
                await self._s.execute(
                    select(ExecutionResourceRow).where(
                        ExecutionResourceRow.session_id == session_id,
                        ExecutionResourceRow.state.in_(["starting", "running", "unknown"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        retained = [
            f"{row.kind}:{row.resource_id}:{row.state}" + (f":{row.purpose}" if row.purpose else "")
            for row in rows
            if row.kind in {"terminal", "monitor", "command", "script"}
        ]
        delegations = (
            (
                await self._s.execute(
                    select(ExecutionDelegationRow).where(
                        ExecutionDelegationRow.session_id == session_id,
                        ExecutionDelegationRow.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        if delegations:
            modes = {row.mode for row in delegations}
            duty_note = (
                "持续值班中（continuous 委托生效）"
                if "continuous" in modes
                else "一次性值班待触发（once 委托生效）"
            )
            retained.extend(f"duty:{row.id}:{row.mode}:{row.goal[:40]}" for row in delegations)
        else:
            duty_note = "保留资源但未委托：事件会被记录，模型不会自动启动。"
        return retained, duty_note

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
        from agent_workbench.persistence.tables import (
            ExecutionResourceRow,
            ToolJobRow,
            ToolOperationRow,
        )

        rows = (
            (
                await self._s.execute(
                    select(ToolJobRow)
                    .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .outerjoin(
                        ExecutionResourceRow, ExecutionResourceRow.resource_id == ToolJobRow.job_id
                    )
                    .where(
                        (ExecutionResourceRow.resource_id.is_(None))
                        | (
                            ExecutionResourceRow.kind.in_(["command", "script"])
                            & ExecutionResourceRow.detail[
                                "completion_role"
                            ].astext.is_distinct_from("resource")
                        )
                    )
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
        # 有限执行资源（命令/脚本）：未完成即"待办"，**不能**仅改称"保留资源"放行。
        # 终端/监视器是长期资源，本身不隐式阻塞（在报告里单独列出）。
        finite = (
            (
                await self._s.execute(
                    select(ExecutionResourceRow).where(
                        ExecutionResourceRow.session_id == session_id,
                        ExecutionResourceRow.kind.in_(["command", "script"]),
                        ExecutionResourceRow.detail["completion_role"].astext.is_distinct_from(
                            "resource"
                        ),
                        ExecutionResourceRow.state.in_(["starting", "running", "unknown"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        return (
            [f"{row.job_id}:{row.state}" for row in rows]
            + [f"operation:{row.operation_id}:{row.status}" for row in standalone]
            + [f"{row.kind}:{row.resource_id}:{row.state}" for row in finite]
        )

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
