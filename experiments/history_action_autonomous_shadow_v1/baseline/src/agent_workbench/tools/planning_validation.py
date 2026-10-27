"""整批检查后一次提交；报告具体步骤的问题，不部分修改计划。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from agent_workbench.contracts.planning import (
    PlanningSource,
    PlanningValidationError,
    StepCompletionClaim,
)
from agent_workbench.domain.planning import (
    check_step_completion,
    completion_is_current,
    resolve_sources,
)
from agent_workbench.persistence.tables import LeadRow


async def validate_plan_steps(
    db: Any, box: Any, previous: list[dict[str, Any]], steps: list[dict[str, Any]], supplied: Any
) -> None:
    old = {step["step_id"]: step for step in previous}
    inputs = {str(item.get("step_id") or f"s{i}"): item for i, item in enumerate(supplied or [], 1)}
    issues = []
    for step in steps:
        identifier = step["step_id"]
        try:
            for raw_id in step.get("lead_ids") or []:
                lead = await db.get(LeadRow, UUID(raw_id))
                if lead is None or lead.session_id != box.session_id:
                    raise ValueError("计划引用的线索不属于当前会话")
            if step.get("evidence_refs"):
                await resolve_sources(
                    db,
                    box.artifacts,
                    box.session_id,
                    [
                        PlanningSource(kind="artifact", id=UUID(value))
                        for value in step["evidence_refs"]
                    ],
                    step_ids={identifier},
                )
            if supplied is None or step.get("status") != "done":
                continue
            if step == old.get(identifier):
                # 沿用不是再次确认：保留历史记录，不让无关修订被旧完成阻塞。
                # FinishGate 仍逐项复查；删除/归档旧完成也必须另行复查。
                continue
            value = inputs.get(identifier, {}).get("completion")
            if value is not None and value != old.get(identifier, {}).get("completion"):
                claim = StepCompletionClaim.model_validate(value)
                step["legacy_completion"] = False
                step["completion"] = await check_step_completion(
                    db,
                    box.artifacts,
                    session_id=box.session_id,
                    run_id=box.run_id,
                    step=step,
                    claim=claim,
                )
            else:
                issue = await completion_is_current(
                    db, box.artifacts, box.session_id, step, run_id=box.run_id
                )
                if not step.get("completion") or issue:
                    raise ValueError(issue or "done 必须提交 completion_spec 与完成依据")
        except ValueError as exc:
            details = dict(exc.details) if isinstance(exc, PlanningValidationError) else {}
            details["target_step_id"] = identifier
            issues.append(
                {
                    "code": exc.code
                    if isinstance(exc, PlanningValidationError)
                    else "invalid_step",
                    "message": "字段不符合规划合同，check 只能由程序生成"
                    if isinstance(exc, ValidationError)
                    else str(exc),
                    "recovery": exc.recovery
                    if isinstance(exc, PlanningValidationError)
                    else "保留无关步骤；沿用已有字段，修正本步骤范围或来源。不要重取已有结果。",
                    **details,
                }
            )
    if issues:
        raise PlanningValidationError(
            "plan_update_rejected",
            "计划未提交："
            + "；".join(f"{item['target_step_id']}：{item['message']}" for item in issues[:8]),
            recovery="整批保持原状。按 step_errors 一次修正；未改变的步骤只需 step_id，程序沿用其余字段。不要重放已执行工具。",
            step_errors=issues[:8],
            remaining_errors=max(0, len(issues) - 8),
        )
