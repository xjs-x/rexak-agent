"""有界的已有结果目录；只读投影，引用在完成提交时再次核对。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.planning import completion_review_reason
from agent_workbench.domain.planning_http import http_receipt
from agent_workbench.persistence.tables import ArtifactRow, ToolOperationRow


async def planning_result_context(
    db: AsyncSession, session_id: UUID, state: dict[str, Any]
) -> dict[str, Any]:
    base = select(ToolOperationRow).where(
        ToolOperationRow.session_id == session_id,
        ToolOperationRow.tool_name.in_(
            ("http_request", "run_command", "run_script", "browser_navigate", "browser_action")
        ),
        ToolOperationRow.status == "succeeded",
    )
    recent = list(
        (await db.execute(base.order_by(ToolOperationRow.created_at.desc()).limit(8))).scalars()
    )
    # 较老的计划引用也保留入口，避免只因结果掉出最近八项就重新执行。
    refs = [
        ref
        for step in state.get("plan") or []
        for ref in (step.get("completion") or {}).get("source_refs", [])
    ]
    artifact_ids = [UUID(ref["id"]) for ref in refs if ref.get("kind") == "artifact"][:24]
    operation_ids = [UUID(ref["id"]) for ref in refs if ref.get("kind") == "operation"][:24]
    if artifact_ids:
        operation_ids.extend(
            value
            for value in (
                await db.execute(
                    select(ArtifactRow.source_operation_id).where(
                        ArtifactRow.session_id == session_id,
                        ArtifactRow.id.in_(artifact_ids),
                        ArtifactRow.source_operation_id.is_not(None),
                    )
                )
            ).scalars()
            if value is not None
        )
    related = list(
        (
            await db.execute(
                base.where(
                    or_(
                        ToolOperationRow.operation_id.in_(
                            [value for value in operation_ids if value]
                        ),
                        ToolOperationRow.execution_context["step_id"].astext
                        == (state.get("current_focus") or {}).get("step_id"),
                    )
                )
                .order_by(ToolOperationRow.created_at.desc())
                .limit(8)
            )
        ).scalars()
    )
    rows = list({row.operation_id: row for row in [*recent, *related]}.values())[:12]
    artifact_rows = (
        await db.execute(
            select(ArtifactRow)
            .where(
                ArtifactRow.session_id == session_id,
                ArtifactRow.source_operation_id.in_([row.operation_id for row in rows]),
                ArtifactRow.availability == "available",
            )
            .order_by(ArtifactRow.created_at, ArtifactRow.id)
        )
    ).scalars()
    artifacts: dict[str, list[str]] = {}
    for artifact in artifact_rows:
        artifacts.setdefault(str(artifact.source_operation_id), []).append(str(artifact.id))
    entries = []
    for row in rows:
        receipt = http_receipt(row)
        entry = {
            "operation_id": str(row.operation_id),
            "tool_call_id": row.tool_call_id,
            "tool": row.tool_name,
            "status": row.status,
            "source_step_id": (row.execution_context or {}).get("step_id"),
            "source_plan_revision": (row.execution_context or {}).get("plan_revision"),
            "artifact_ids": artifacts.get(str(row.operation_id), [])[:3],
            "operation_created_at": row.created_at.isoformat(),
        }
        if receipt:
            entry.update(
                {
                    key: receipt[key]
                    for key in ("method", "url", "identity", "authentication", "status_code")
                }
            )
        entries.append(entry)
    outstanding = [
        {
            "step_id": step["step_id"],
            "status": step.get("status"),
            "description": str(step.get("description") or "")[:160],
            "done_when": (step.get("completion_spec") or {}).get("done_when")
            or step.get("done_when"),
            "http_checks": (step.get("completion_spec") or {}).get("http_checks", []),
            "review": completion_review_reason(step)
            or (
                "旧完成检查需引用已有来源补充范围，不要自动重测"
                if (step.get("completion") or {}).get("check", {}).get("version") == 1
                else None
            ),
        }
        for step in state.get("plan") or []
        if step.get("status") not in {"done", "dropped"}
        or (step.get("completion") or {}).get("check", {}).get("version") == 1
    ][:8]
    for work in outstanding:
        checks = work["http_checks"]
        work["existing_matches"] = [
            {
                "method": check["method"],
                "url": check["url"],
                "operation_ids": [
                    entry["operation_id"]
                    for entry in entries
                    if entry.get("method") == check["method"]
                    and entry.get("url") == check["url"]
                    and (not check.get("identity") or check["identity"] == entry.get("identity"))
                    and (
                        not check.get("authentication")
                        or check["authentication"] == entry.get("authentication")
                    )
                ],
            }
            for check in checks
        ]
    return {
        "available_results": entries,
        "work_to_close": outstanding,
        "note": "已有结果可跨任务复用，程序自动记 supporting，原归属不变。目录只表示已有回执；来源和完成范围在提交时复查，状态码不等于漏洞。",
        "update_hint": "steps_json 仍是完整步骤清单；未改的步骤只写 step_id 即可沿用。关键增项、切换或受阻时填简短 assessment_json，普通请求不填。",
    }
