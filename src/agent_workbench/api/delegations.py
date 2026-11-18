"""本地控制台读取子任务投影；浏览器不直接连接 A2A。"""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from agent_workbench.config.settings import get_settings
from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.contracts.delegation import (
    DelegationList,
    DelegationRead,
    WorkerTrajectoryRead,
    WorkerVisibleEvent,
)
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    DelegationCollaborationRow,
    SessionRow,
    ToolOperationRow,
)

router = APIRouter()
VISIBLE_MESSAGE_MEDIA = "application/json"


class WorkerVisibleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: UUID
    task_id: str = Field(min_length=1, max_length=128)
    context_id: UUID
    sequence: int = Field(gt=0, le=2**31 - 1)
    message_json: str = Field(min_length=1, max_length=2_000_000)


def _body_identity(delegation_id: UUID, sequence: int) -> str:
    return f"pi-visible:{delegation_id}:{sequence}"


@router.post("/internal/delegations/{delegation_id}/visible-body", include_in_schema=False)
async def archive_worker_visible_body(delegation_id: UUID, body: WorkerVisibleBody, request: Request) -> dict:
    from agent_workbench.api.redaction import redact_api_value

    settings = get_settings()
    secret = settings.ultra_worker_token or read_worker_config(settings).get("token", "")
    try:
        payload_hash = hashlib.sha256(body.message_json.encode()).hexdigest()
    except UnicodeEncodeError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 可见消息不是有效 UTF-8") from exc
    identity = f"visible-body:v1:{delegation_id}:{body.task_id}:{body.worker_id}:{body.context_id}:{body.sequence}:{payload_hash}"
    expected = hmac.new(secret.encode(), identity.encode(), hashlib.sha256).hexdigest()
    if len(secret) < 24 or not hmac.compare_digest(expected, request.headers.get("authorization", "").removeprefix("Bearer ")):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker 轨迹归档凭据无效")

    try:
        message = json.loads(body.message_json)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 可见消息格式无效") from exc
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 可见消息缺少内容")
    # Keep the archive restricted to the same visible content as the timeline.
    message = {"content": [part for part in message["content"] if isinstance(part, dict)
                           and part.get("type") in ("text", "toolCall")]}
    message = redact_api_value(message)
    try:
        data = json.dumps(message, ensure_ascii=False).encode()
    except UnicodeEncodeError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 可见消息不是有效 UTF-8") from exc
    if len(data) > 4_000_000:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 可见消息超出归档大小")
    database = get_database()
    async with database.session() as db:
        delegation = await db.get(AgentDelegationRow, delegation_id)
        worker = (await db.execute(text("""SELECT task_id,worker_id,phase FROM ultra_worker.requests
              WHERE request_id=:id"""), {"id": delegation_id})).mappings().one_or_none()
        if (not delegation or not worker or worker["phase"] != "running"
                or worker["task_id"] != body.task_id or worker["worker_id"] != body.worker_id
                or delegation.context_id != body.context_id):
            raise ApiError(ErrorCode.NOT_FOUND, "未找到匹配的 Worker 委派")
        await _authorized_delegation(db, delegation.parent_session_id, delegation_id)
        session_id = delegation.parent_session_id

    store = ArtifactStore(settings)
    stored = await asyncio.to_thread(store.write_bytes, data, media_type=VISIBLE_MESSAGE_MEDIA)
    artifact_id = uuid5(delegation_id, f"visible-body:{body.sequence}:{stored.sha256}")
    async with database.transaction() as db:
        await db.execute(insert(ArtifactRow).values(
            id=artifact_id, sha256=stored.sha256, byte_size=stored.byte_size, media_type=stored.media_type,
            relative_key=stored.relative_key, session_id=session_id, source_operation_id=None,
            sensitivity="internal", availability="available", captured_at=stored.captured_at,
            captured_by_identity=_body_identity(delegation_id, body.sequence), summary="Worker 可见消息正文",
        ).on_conflict_do_nothing(index_elements=[ArtifactRow.id]))
    preview = "\n".join(part["text"] for part in message["content"] if isinstance(part, dict)
                        and part.get("type") == "text" and isinstance(part.get("text"), str))
    return {"artifact_id": str(artifact_id), "preview": redact_api_value(preview)[:3000]}


async def _authorized_delegation(db, session_id: UUID, delegation_id: UUID) -> AgentDelegationRow:
    parent = await db.get(SessionRow, session_id)
    if not parent or parent.owner_id != get_settings().owner_id or parent.status == "deleted":
        raise ApiError(ErrorCode.NOT_FOUND, "Session 不存在")
    row = await db.get(AgentDelegationRow, delegation_id)
    if row is None or row.parent_session_id != session_id:
        raise ApiError(ErrorCode.NOT_FOUND, "Worker 委派不存在")
    return row


def _visible_row(row) -> dict:
    from agent_workbench.api.redaction import redact_api_value

    message = redact_api_value(row["message"])
    encoded = json.dumps(message, ensure_ascii=False)
    if len(encoded) > 4000:
        content = message.get("content")
        if isinstance(content, list):
            preview = "\n".join(part["text"] for part in content if isinstance(part, dict)
                                and part.get("type") == "text" and isinstance(part.get("text"), str))
        else:
            preview = content if isinstance(content, str) else encoded
        metadata = {key: message[key] for key in (
            "toolCallId", "toolName", "status", "isError", "startedAt", "finishedAt", "artifactRefs",
            "body_artifact_id", "body_unavailable",
        ) if key in message}
        preview_only = "preview" in message and "content" not in message and "args" not in message
        if preview_only and isinstance(message["preview"], str):
            preview = message["preview"]
        message = {**metadata, "truncated": bool(message.get("truncated", not preview_only)), "preview": preview[:3000]}
    return {"sequence": row["sequence"], "revision": row["revision"],
            "phase": row["phase"], "role": row["role"], "message": message,
            "at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}


@router.get("/sessions/{session_id}/delegations/{delegation_id}/trajectory", response_model=WorkerTrajectoryRead, tags=["ultra"])
async def worker_trajectory(session_id: UUID, delegation_id: UUID, request: Request,
                            after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
                            before: int | None = Query(None, gt=0)) -> dict:
    from agent_workbench.api.redaction import redact_api_value
    from agent_workbench.api.routes import get_auth

    get_auth().require_session(request)
    async with get_database().session() as db:
        delegation = await _authorized_delegation(db, session_id, delegation_id)
        if before is None:
            statement = """SELECT sequence,revision,phase,role,message,created_at,updated_at
                FROM ultra_worker.visible_events WHERE request_id=:id AND sequence>:boundary
                ORDER BY sequence LIMIT :limit"""
            boundary = after
        else:
            statement = """SELECT sequence,revision,phase,role,message,created_at,updated_at
                FROM ultra_worker.visible_events WHERE request_id=:id AND sequence<:boundary
                ORDER BY sequence DESC LIMIT :limit"""
            boundary = before
        page = (await db.execute(text(statement),
            {"id": delegation_id, "boundary": boundary, "limit": limit + 1})).mappings().all()
        findings = (await db.scalars(select(DelegationCollaborationRow).where(
            DelegationCollaborationRow.delegation_id == delegation_id,
            DelegationCollaborationRow.direction == "finding")
            .order_by(DelegationCollaborationRow.sequence).limit(100))).all()
        shown = page[:limit]
        last_change = max(shown, key=lambda row: (row["updated_at"], row["sequence"]), default=None)
        stream_cursor = f'{last_change["updated_at"].isoformat()}|{last_change["sequence"]}' if last_change else None
        return {"delegation": redact_api_value(DelegationRead.model_validate(delegation).model_dump(mode="json")),
                "resume_from_delegation_id": delegation.handoff_snapshot.get("resume_from_delegation_id"),
                "events": [_visible_row(row) for row in (reversed(shown) if before else shown)],
                "next_cursor": shown[-1]["sequence"] if shown else boundary,
                "has_more": len(page) > limit,
                "stream_cursor": stream_cursor,
                "history_incomplete": delegation.state in {"completed", "failed", "cancelled"} and not page and after == 0,
                "findings": [{"id": str(row.id), "at": row.created_at.isoformat(),
                              "summary": redact_api_value(row.payload.get("summary", "")),
                              "evidence_refs": redact_api_value(row.payload.get("evidence_refs", [])),
                              "delivery_status": row.delivery_status} for row in findings]}


@router.get("/sessions/{session_id}/delegations/{delegation_id}/trajectory/{sequence}", response_model=WorkerVisibleEvent, tags=["ultra"])
async def worker_trajectory_message(session_id: UUID, delegation_id: UUID, sequence: int,
                                    request: Request) -> dict:
    from agent_workbench.api.redaction import redact_api_value
    from agent_workbench.api.routes import get_auth

    get_auth().require_session(request)
    async with get_database().session() as db:
        await _authorized_delegation(db, session_id, delegation_id)
        row = (await db.execute(text("""SELECT sequence,revision,phase,role,message,created_at,updated_at
            FROM ultra_worker.visible_events WHERE request_id=:id AND sequence=:sequence"""),
            {"id": delegation_id, "sequence": sequence})).mappings().one_or_none()
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Worker 消息不存在")
        message = row["message"]
        reference = message.get("body_artifact_id")
        if reference is not None:
            try:
                artifact = await db.get(ArtifactRow, UUID(reference))
            except (ValueError, TypeError) as exc:
                raise ApiError(ErrorCode.CONFLICT, "Worker 消息正文引用无效") from exc
            if (not artifact or artifact.session_id != session_id or artifact.availability != "available"
                    or artifact.captured_by_identity != _body_identity(delegation_id, sequence)):
                raise ApiError(ErrorCode.NOT_FOUND, "Worker 消息正文不可用")
            key, digest = artifact.relative_key, artifact.sha256
        else:
            return {**_visible_row(row), "message": redact_api_value(message)}
    store = ArtifactStore(get_settings())

    def read_body() -> dict:
        if not store.verify(key, digest):
            raise ApiError(ErrorCode.CONFLICT, "Worker 消息正文工件校验失败")
        return json.loads(store.read_bytes(key))

    return {**_visible_row(row), "message": redact_api_value(await asyncio.to_thread(read_body))}


@router.get("/sessions/{session_id}/delegations/{delegation_id}/output/{artifact_id}", tags=["ultra"])
async def worker_output(session_id: UUID, delegation_id: UUID, artifact_id: UUID,
                        request: Request, offset: int = Query(0, ge=0)) -> dict:
    from agent_workbench.api.routes import get_auth

    get_auth().require_session(request)
    async with get_database().session() as db:
        delegation = await _authorized_delegation(db, session_id, delegation_id)
        artifact = await db.get(ArtifactRow, artifact_id)
        operation = await db.get(ToolOperationRow, artifact.source_operation_id) if artifact else None
        if (artifact is None or operation is None or artifact.session_id != session_id
                or operation.session_id != session_id or operation.run_id != delegation.parent_run_id
                or (operation.execution_context or {}).get("agent_delegation_id") != str(delegation_id)
                or artifact.availability != "available"):
            raise ApiError(ErrorCode.NOT_FOUND, "Worker 输出不存在")
        key, sha256, media_type = artifact.relative_key, artifact.sha256, artifact.media_type
    store = ArtifactStore(get_settings())
    if not store.verify(key, sha256):
        raise ApiError(ErrorCode.CONFLICT, "Worker 输出工件不可用")
    redacted = store.render_redacted_view(key, media_type=media_type)
    return {"artifact_id": str(artifact_id), "offset": offset,
            "content": redacted[offset:offset + 16384],
            "next_offset": offset + 16384 if offset + 16384 < len(redacted) else None}


@router.get("/sessions/{session_id}/delegations/{delegation_id}/events", tags=["ultra"])
async def worker_events(session_id: UUID, delegation_id: UUID, request: Request,
                        after: str = "") -> StreamingResponse:
    from agent_workbench.api.routes import get_auth

    get_auth().require_session(request)
    async with get_database().session() as db:
        await _authorized_delegation(db, session_id, delegation_id)
    cursor = request.headers.get("last-event-id") or after
    try:
        stamp, sequence = cursor.rsplit("|", 1)
        since, last_sequence = datetime.fromisoformat(stamp), int(sequence)
        if since.tzinfo is None:
            raise ValueError("timestamp needs timezone")
    except (ValueError, TypeError):
        since, last_sequence = datetime(1970, 1, 1, tzinfo=UTC), 0

    async def stream():
        nonlocal since, last_sequence
        while not await request.is_disconnected():
            async with get_database().session() as db:
                rows = (await db.execute(text("""SELECT sequence,revision,phase,role,message,created_at,updated_at
                    FROM ultra_worker.visible_events WHERE request_id=:id
                      AND (updated_at,sequence) > (:since,:sequence)
                    ORDER BY updated_at,sequence LIMIT 100"""),
                    {"id": delegation_id, "since": since, "sequence": last_sequence})).mappings().all()
            for row in rows:
                since, last_sequence = row["updated_at"], row["sequence"]
                event_id = f"{since.isoformat()}|{last_sequence}"
                yield f"id: {event_id}\ndata: {json.dumps(_visible_row(row), ensure_ascii=False)}\n\n"
            if len(rows) < 100:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.7)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sessions/{session_id}/delegations", response_model=DelegationList, tags=["ultra"])
async def list_delegations(session_id: UUID, request: Request) -> DelegationList:
    from agent_workbench.api.redaction import redact_api_value
    from agent_workbench.api.routes import get_auth

    get_auth().require_session(request)
    async with get_database().session() as db:
        parent = await db.get(SessionRow, session_id)
        if not parent or parent.owner_id != get_settings().owner_id or parent.status == "deleted":
            raise ApiError(ErrorCode.NOT_FOUND, "Session 不存在")
        rows = (await db.scalars(select(AgentDelegationRow).where(
            AgentDelegationRow.parent_session_id == session_id,
        ).order_by(AgentDelegationRow.created_at.desc()).limit(100))).all()
        return DelegationList(tasks=[DelegationRead.model_validate(redact_api_value(DelegationRead.model_validate(row).model_dump(mode="json"))) for row in rows])
