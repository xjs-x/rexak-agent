"""只清理用户确认的会话；数据库与 checkpoint 在同一事务内删除。"""

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.repositories import ExecutionResourceRepository
from agent_workbench.persistence.tables import (
    APP_METADATA,
    ArtifactRow,
    AttachmentAnalysisRow,
    AttachmentRow,
    LeadRow,
    LeadWeaknessLinkRow,
    ModelCallRow,
    ObservationRow,
    RunRow,
    SessionRow,
    SkillVersionRow,
    ToolJobRow,
    ToolOperationRow,
)


async def delete_chat(db: AsyncSession, session_id: UUID, settings: Settings) -> dict:
    row = await db.scalar(select(SessionRow).where(
        SessionRow.id == session_id, SessionRow.owner_id == settings.owner_id
    ).with_for_update())
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "对话不存在")
    runs = list((await db.scalars(select(RunRow).where(
        RunRow.session_id == session_id).with_for_update())).all())
    resources = await ExecutionResourceRepository(db).active_for_session(session_id)
    operations = select(ToolOperationRow.operation_id).where(ToolOperationRow.session_id == session_id)
    jobs = list((await db.scalars(select(ToolJobRow).where(
        ToolJobRow.operation_id.in_(operations),
        or_(ToolJobRow.state.not_in(["succeeded", "failed", "cancelled"]),
            ToolJobRow.sync_status == "uncertain"),
    ))).all())
    busy = [r for r in runs if r.status in {"queued", "running", "waiting_tool", "pausing", "cancelling"}
            or (r.owner and r.lease_until and r.lease_until > datetime.now(UTC))]
    if resources or jobs or busy:
        raise ApiError(ErrorCode.CONFLICT,
                       "对话仍有活动运行、执行任务或未确认关闭的资源。请先停止并确认后再删除。",
                       details={"required_field": "terminate_resources" if resources else "stop_run",
                                "active_resources": [f"{r.kind}:{r.resource_id}:{r.state}" for r in resources],
                                "active_runs": [str(r.id) for r in busy],
                                "active_jobs": [str(j.job_id) for j in jobs]})

    # 标识符只取部署配置，并使用方言引用；值始终绑定，不拼接用户数据。
    quote = db.bind.dialect.identifier_preparer.quote_identifier  # type: ignore[union-attr]
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        await db.execute(text(f"DELETE FROM {quote(settings.checkpoint_schema)}.{quote(table)} "
                              "WHERE thread_id=:thread"), {"thread": row.thread_id})
    for table in ("checkpoint_write_receipts", "checkpoint_heads"):
        await db.execute(text(f"DELETE FROM {quote(settings.app_schema)}.{quote(table)} "
                              "WHERE thread_id=:thread"), {"thread": row.thread_id})
    memory_schema = quote(settings.memory_schema)
    scoped_owner = f"{settings.owner_id}:session:{session_id}"
    await db.execute(text(f"DELETE FROM {memory_schema}.store WHERE starts_with(prefix, :prefix)"),
                     {"prefix": scoped_owner + "."})
    # 旧共享经验可能聚合多个会话的来源；只移除被删会话的来源，不能删掉他人贡献。
    run_ids = {str(r.id) for r in runs}
    artifact_ids = {str(a) for a in (await db.scalars(select(ArtifactRow.id).where(
        ArtifactRow.session_id == session_id))).all()}
    observation_ids = {str(a) for a in (await db.scalars(select(ObservationRow.id).where(
        ObservationRow.session_id == session_id))).all()}

    def own_event(value: str) -> bool:
        parts = value.split(":")
        return len(parts) > 1 and (
            (parts[0] == "tool" and parts[1] in run_ids)
            or (parts[0] == "observation" and parts[1] in observation_ids)
        )

    records = (await db.execute(text(
        f"SELECT prefix,key,value FROM {memory_schema}.store "
        "WHERE starts_with(prefix,:prefix) FOR UPDATE"),
        {"prefix": settings.owner_id + "."})).mappings().all()
    for record in records:
        value = dict(record["value"])
        events = list(value.get("source_event_ids") or [])
        refs = list(value.get("source_artifact_ids") or [])
        affected = (value.get("source_run_id") in run_ids
                    or own_event(str(value.get("source_event_id", "")))
                    or any(own_event(str(e)) for e in events)
                    or bool(artifact_ids.intersection(refs)))
        if not affected:
            continue
        remaining_events = [e for e in events if not own_event(str(e))]
        remaining_refs = [r for r in refs if r not in artifact_ids]
        params = {"prefix": record["prefix"], "key": record["key"]}
        if remaining_events or remaining_refs:
            value["source_event_ids"] = remaining_events
            value["source_artifact_ids"] = remaining_refs
            if value.get("source_run_id") in run_ids:
                value["source_run_id"] = None
            if own_event(str(value.get("source_event_id", ""))):
                value.pop("source_event_id", None)
            await db.execute(text(f"UPDATE {memory_schema}.store SET value=CAST(:value AS jsonb) "
                                  "WHERE prefix=:prefix AND key=:key"),
                             {**params, "value": json.dumps(value, ensure_ascii=False)})
        else:
            await db.execute(text(f"DELETE FROM {memory_schema}.store "
                                  "WHERE prefix=:prefix AND key=:key"), params)
    await db.execute(delete(ToolJobRow).where(ToolJobRow.operation_id.in_(operations)))
    await db.execute(delete(LeadWeaknessLinkRow).where(LeadWeaknessLinkRow.session_id == session_id))
    await db.execute(update(LeadRow).where(LeadRow.session_id == session_id).values(merged_into_id=None))
    await db.execute(delete(AttachmentAnalysisRow).where(AttachmentAnalysisRow.session_id == session_id))
    await db.execute(delete(AttachmentRow).where(AttachmentRow.session_id == session_id))
    await db.execute(delete(ModelCallRow).where(ModelCallRow.session_id == session_id))
    # 无外键的台账也必须清理，不能只依赖 sessions 的 CASCADE。
    for table in APP_METADATA.tables.values():
        if table.name in {"sessions", "identities", "artifacts", "attachments", "attachment_analyses"}:
            continue
        if "session_id" in table.c and not table.foreign_keys:
            await db.execute(delete(table).where(table.c.session_id == session_id))
    await db.execute(update(SessionRow).where(SessionRow.id == session_id).values(active_identity_id=None))
    await db.execute(delete(SessionRow).where(SessionRow.id == session_id))
    # 技能库引用的工件属于共享资料；解除会话归属，保留共享引用及 CAS 原件。
    shared = select(SkillVersionRow.artifact_id)
    await db.execute(update(ArtifactRow).where(
        ArtifactRow.session_id == session_id, ArtifactRow.id.in_(shared)).values(session_id=None))
    await db.execute(delete(ArtifactRow).where(ArtifactRow.session_id == session_id))
    return {"id": str(session_id), "deleted": True, "cancellation_pending": False}
