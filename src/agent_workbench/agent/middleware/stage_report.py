"""关键领域转折后的阶段交付；普通工具结果不触发，失败不回滚探索。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.domain.reports import create_report_snapshot
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.observability.logging import get_logger
from agent_workbench.persistence.tables import FindingRow, ObservationRow

log = get_logger("agent_workbench.stage_report")


def stage_metadata(
    state: dict[str, Any], *, run_id: str, trigger: str, signature: str
) -> dict[str, Any]:
    return redact_api_value(
        {
            "stage_key": f"{run_id}:{trigger}:{signature}",
            "trigger": trigger,
            "source": "official_graph_state_projection",
            "atomic_with_domain": False,
            "plan_revision": int(state.get("plan_revision") or 0),
            "completed_decision_rounds": int(state.get("completed_decision_rounds") or 0),
            "goal_progress": state.get("plan") or [],
            "focus": state.get("current_focus"),
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
            "note": "模型解释保留候选语义；阶段报告不等于任务结束。",
        }
    )


class StageReportMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        toolbox = context.toolbox
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
        completed = [
            step.get("step_id") for step in state.get("plan") or [] if step.get("status") == "done"
        ]
        signals = {"observations": observations, "findings": findings, "completed_steps": completed}
        signature = hashlib.sha256(json.dumps(signals, sort_keys=True).encode()).hexdigest()
        previous = state.get("report_progress_signature")
        if previous is None or previous == signature:
            return {"report_progress_signature": signature} if previous is None else None
        metadata = stage_metadata(
            dict(state),
            run_id=str(toolbox.run_id),
            trigger="domain_turning_point",
            signature=signature,
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
            return {"report_retry_key": metadata["stage_key"]}
        return {"report_progress_signature": signature, "report_retry_key": None}
