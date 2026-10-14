"""关键领域转折后的阶段交付；普通工具结果不触发，失败不回滚探索。

与压缩**解耦**（§8.1）：报告内容直接来自"当前判断 + 领域记录 + 执行台账"，
不依赖一次成功的摘要压缩。阶段签名包含有效领域版本、完成步骤与最近判断修订；
普通模型流与新工件 hash **不**触发报告。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from sqlalchemy import func, select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.focus import assessment_applies
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.domain.reports import create_report_snapshot
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.observability.logging import get_logger
from agent_workbench.persistence.tables import (
    FindingRow,
    LeadRow,
    ObservationRow,
    ToolOperationRow,
)

log = get_logger("agent_workbench.stage_report")


def stage_metadata(
    state: dict[str, Any],
    *,
    run_id: str,
    trigger: str,
    signature: str,
    covered_position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """阶段快照的元数据。

    明确区分**生成时间**与**覆盖到的位置**：图与业务数据库不在同一事务，
    `atomic_with_domain=false` 如实标注；旧快照不是自动覆盖最新活动。
    """
    now = datetime.now(UTC).isoformat()
    assessment = state.get("work_assessment") or {}
    return redact_api_value(
        {
            "stage_key": f"{run_id}:{trigger}:{signature}",
            "trigger": trigger,
            "source": "official_graph_state_projection",
            "atomic_with_domain": False,
            "generated_at": now,
            "plan_revision": int(state.get("plan_revision") or 0),
            "completed_decision_rounds": int(state.get("completed_decision_rounds") or 0),
            "goal_progress": state.get("plan") or [],
            "focus": state.get("current_focus"),
            "work_assessment": assessment or None,
            "current_focus_summary": {
                "step_id": (state.get("current_focus") or {}).get("step_id"),
                "intent": (state.get("current_focus") or {}).get("intent"),
                "source": (state.get("current_focus") or {}).get("source"),
            },
            "assessment_applies_to_focus": assessment_applies(state),
            "next_action": assessment.get("next_action") if assessment_applies(state) else None,
            "model_explanation": {
                key: (state.get("work_summary") or {}).get(key)
                for key in (
                    "current_focus",
                    "key_findings",
                    "failed_approaches",
                    "open_questions",
                    "next_steps",
                )
            },
            "next_steps": (state.get("work_summary") or {}).get("next_steps") or [],
            "covered_position": covered_position
            or {
                "plan_revision": int(state.get("plan_revision") or 0),
                "completed_decision_rounds": int(state.get("completed_decision_rounds") or 0),
                "work_assessment_revision": int(assessment.get("revision") or 0),
            },
            "note": (
                "模型解释保留候选语义；阶段报告不等于任务结束。"
                "没有模型判断时如实写『尚未记录判断』，不由模板补成阴性结论。"
            ),
        }
    )


async def _domain_signals(toolbox: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取**当前适用**的领域版本与覆盖位置（只做只读投影）。"""
    async with toolbox.db.session() as db:
        observations = [
            str(item)
            for item in (
                await db.execute(
                    select(ObservationRow.id)
                    .where(ObservationRow.session_id == toolbox.session_id)
                    .order_by(ObservationRow.id)
                )
            ).scalars()
        ]
        findings = [
            (str(row.id), row.revision)
            for row in (
                await db.execute(
                    select(FindingRow)
                    .where(FindingRow.session_id == toolbox.session_id)
                    .order_by(FindingRow.id)
                )
            ).scalars()
        ]
        leads = [
            (str(row.id), row.revision)
            for row in (
                await db.execute(
                    select(LeadRow)
                    .where(LeadRow.session_id == toolbox.session_id)
                    .order_by(LeadRow.id)
                )
            ).scalars()
        ]
        latest_operation = (
            await db.execute(
                select(func.max(ToolOperationRow.created_at)).where(
                    ToolOperationRow.session_id == toolbox.session_id
                )
            )
        ).scalar_one()
    signals = {"observations": observations, "findings": findings, "leads": leads}
    covered = {
        "domain_versions": {
            "observations": dict.fromkeys(observations, 1),
            "findings": dict(findings),
            "leads": dict(leads),
        },
        "latest_domain_records": {
            "observations": observations[-1] if observations else None,
            "findings": findings[-1][0] if findings else None,
            "leads": leads[-1][0] if leads else None,
        },
        "latest_operation_at": str(latest_operation) if latest_operation else None,
        "domain_snapshot_at": datetime.now(UTC).isoformat(),
    }
    return signals, covered


class StageReportMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        toolbox = context.toolbox
        domain_signals, covered = await _domain_signals(toolbox)
        completed = [
            step.get("step_id") for step in state.get("plan") or [] if step.get("status") == "done"
        ]
        assessment = state.get("work_assessment") or {}
        signals = {
            **domain_signals,
            "completed_steps": completed,
            "assessment_revision": int(assessment.get("revision") or 0),
        }
        signature = hashlib.sha256(json.dumps(signals, sort_keys=True).encode()).hexdigest()
        previous = state.get("report_progress_signature")
        substantive = bool(
            domain_signals["observations"]
            or domain_signals["findings"]
            or domain_signals["leads"]
            or completed
            or signals["assessment_revision"]
        )
        if previous is None and not substantive:
            # 确实还没有实质内容：设基线，避免每轮都尝试生成空报告。
            return {"report_progress_signature": signature}
        if previous == signature:
            return None
        metadata = stage_metadata(
            dict(state),
            run_id=str(toolbox.run_id),
            trigger="domain_turning_point",
            signature=signature,
            covered_position={
                **covered,
                "plan_revision": int(state.get("plan_revision") or 0),
                "completed_decision_rounds": int(state.get("completed_decision_rounds") or 0),
                "work_assessment_revision": signals["assessment_revision"],
                "graph_read_at": datetime.now(UTC).isoformat(),
            },
        )
        try:
            await create_report_snapshot(
                toolbox.db,
                ArtifactStore(toolbox.db.settings),
                toolbox.session_id,
                stage_metadata=metadata,
            )
        except Exception as exc:
            log.warning(
                "stage_report_failed_retryable",
                run_id=str(toolbox.run_id),
                stage_key=metadata["stage_key"],
                error_type=type(exc).__name__,
            )
            # 生成失败保留已有报告，不回滚探索；原 stage_key 可按原版本重试。
            return {"report_retry_key": metadata["stage_key"]}
        return {"report_progress_signature": signature, "report_retry_key": None}
