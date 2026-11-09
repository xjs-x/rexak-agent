"""Bounded, read-only projection of one authorized Worker run."""

import json
from datetime import UTC, datetime

from sqlalchemy import select, text

from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    DelegationCollaborationRow,
    ToolOperationRow,
)


async def worker_progress(db, delegation: AgentDelegationRow, *, cursor: int | None = None,
                          limit: int = 5) -> dict:
    """Caller must authorize the delegation against its Session and Run first."""
    where = (ToolOperationRow.run_id == delegation.parent_run_id,
             ToolOperationRow.session_id == delegation.parent_session_id,
             ToolOperationRow.execution_context["agent_delegation_id"].astext == str(delegation.id),
             ToolOperationRow.execution_context["worker_tool_aggregate"].as_boolean().is_(True))
    operations = (await db.scalars(select(ToolOperationRow).where(*where)
                  .order_by(ToolOperationRow.created_at.desc(), ToolOperationRow.operation_id.desc()).limit(6))).all()
    recent = operations[:5]
    artifacts = (await db.scalars(select(ArtifactRow).join(ToolOperationRow,
        ArtifactRow.source_operation_id == ToolOperationRow.operation_id).where(
        *where, ArtifactRow.session_id == delegation.parent_session_id,
        ArtifactRow.availability == "available")
        .order_by(ArtifactRow.captured_at.desc(), ArtifactRow.id.desc()).limit(5))).all()
    finding = await db.scalar(select(DelegationCollaborationRow).where(
        DelegationCollaborationRow.delegation_id == delegation.id,
        DelegationCollaborationRow.direction == "finding")
        .order_by(DelegationCollaborationRow.sequence.desc()).limit(1))
    elapsed = max(0, int((datetime.now(UTC) - delegation.created_at).total_seconds()))
    result = {"objective": delegation.objective[:4000], "state": delegation.state,
              "elapsed_seconds": elapsed,
              "recent_operations": [{"operation_id": str(row.operation_id), "tool": row.tool_name,
                  "description": row.tool_name.removeprefix("pi."), "status": row.status,
                  "at": row.created_at.isoformat()} for row in recent],
              "recent_artifacts": [{"artifact_id": str(row.id), "source_operation_id": str(row.source_operation_id),
                  "description": (row.summary or row.media_type)[:160], "at": row.captured_at.isoformat()}
                  for row in artifacts],
              "latest_finding": {"event_id": str(finding.id), "summary": redact_api_value(finding.payload.get("summary", ""))[:500],
                  "at": finding.created_at.isoformat()} if finding else None,
              "more_operations": len(operations) > 5}
    # New Worker history is optional; old runs have no retroactive records.
    if cursor is None:
        rows = (await db.execute(text("SELECT sequence FROM ultra_worker.visible_events WHERE request_id=:id ORDER BY sequence DESC LIMIT 1"),
                                 {"id": delegation.id})).all()
        result["visible_history"] = {"available": bool(rows), "next_cursor": 0 if rows else None,
                                     "has_more": bool(rows)}
        return result
    page = (await db.execute(text("""SELECT sequence,role,message,created_at FROM ultra_worker.visible_events
        WHERE request_id=:id AND sequence>:cursor ORDER BY sequence LIMIT :limit"""),
        {"id": delegation.id, "cursor": cursor, "limit": limit + 1})).mappings().all()
    shown = page[:limit]
    def bounded_message(value):
        safe = redact_api_value(value)
        encoded = json.dumps(safe, ensure_ascii=False)
        return safe if len(encoded) <= 3000 else {"truncated": True, "preview": encoded[:2400]}

    result["visible_history"] = {"available": bool(page) or cursor > 0,
        "events": [{"sequence": row["sequence"], "role": row["role"],
            "message": bounded_message(row["message"]), "at": row["created_at"].isoformat(),
            "source": "ultra_worker.visible_events"} for row in shown],
        "next_cursor": shown[-1]["sequence"] if shown else None, "has_more": len(page) > limit}
    return result
