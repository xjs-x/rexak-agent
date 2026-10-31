"""附件 REST：原始流上传有界，写操作沿用本地会话、Origin 和 CSRF。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, BinaryIO, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.attachments import AttachmentPage, AttachmentRead, AttachmentSlice
from agent_workbench.contracts.binary_analysis import AttachmentCapabilities, BinaryAnalysisState
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.attachments import (
    expire_temporary,
    file_kind,
    list_metadata,
    load_attachment,
    metadata,
    parse_attachment,
    read_slice,
    require_session,
    validate_filename,
)
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import AttachmentAnalysisRow, AttachmentRow

router = APIRouter(prefix="/api/sessions/{session_id}/attachments", tags=["attachments"])
capabilities_router = APIRouter(prefix="/api/attachments", tags=["attachments"])
_parsers = asyncio.Semaphore(2)


def authorize(request: Request, *, write: bool = False) -> None:
    from agent_workbench.api.routes import get_auth

    auth = get_auth()
    ticket = auth.require_session(request)
    if write:
        auth.check_origin(request)
        auth.require_csrf(request, ticket)


@capabilities_router.get("/capabilities", response_model=AttachmentCapabilities)
async def attachment_capabilities(request: Request) -> AttachmentCapabilities:
    authorize(request)
    cfg = get_settings()
    return AttachmentCapabilities(max_bytes=cfg.attachment_max_bytes,
        binary_max_bytes=cfg.binary_attachment_max_bytes,
        max_per_message=cfg.attachment_max_per_message, binary_formats=["PE", "ELF"])


def binary_header(spool: BinaryIO, size: int) -> str | None:
    """上传分级仅做定长头校验；PE/ELF 结构解析仍在隔离进程。"""
    import struct

    spool.seek(0)
    header = spool.read(64)
    if header.startswith(b"MZ") and len(header) == 64:
        offset = struct.unpack_from("<I", header, 60)[0]
        if 64 <= offset <= size - 26:
            spool.seek(offset)
            pe = spool.read(26)
            if pe[:4] == b"PE\0\0" and pe[24:26] in {b"\x0b\x01", b"\x0b\x02"}:
                return "PE"
    if (header.startswith(b"\x7fELF") and len(header) >= 52
        and header[4:7] in {b"\x01\x01\x01", b"\x01\x02\x01", b"\x02\x01\x01", b"\x02\x02\x01"}):
        return "ELF"
    return None


@router.post("", response_model=AttachmentRead, status_code=201)
async def upload_attachment(
    session_id: UUID,
    request: Request,
    filename: Annotated[str, Query(max_length=240)],
    upload_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AttachmentRead:
    authorize(request, write=True)
    validate_filename(filename)
    cfg = get_settings()
    db = get_database()
    store = ArtifactStore(cfg)
    async with db.session() as session:
        await require_session(session, session_id, cfg)
    length = request.headers.get("content-length")
    hard_limit = max(cfg.attachment_max_bytes, cfg.binary_attachment_max_bytes)
    if length and (not length.isdecimal() or int(length) > hard_limit):
        raise ApiError(ErrorCode.VALIDATION_FAILED, "文件超过上传大小限制")
    # 原始字节流协议，避免 multipart 在鉴权/大小检查前缓存任意大正文。
    cfg.tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=cfg.tmp_root) as spool:
        size = 0
        digest = hashlib.sha256()
        prefix = b""
        async for chunk in request.stream():
            size += len(chunk)
            if size > hard_limit:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "文件超过上传大小限制")
            prefix = (prefix + chunk)[:32] if len(prefix) < 32 else prefix
            spool.write(chunk)
            digest.update(chunk)
        extension, media_type = file_kind(filename, prefix)
        detected_binary = binary_header(cast(BinaryIO, spool), size)
        actual_limit = cfg.binary_attachment_max_bytes if detected_binary else cfg.attachment_max_bytes
        if size > actual_limit:
            raise ApiError(ErrorCode.VALIDATION_FAILED, f"文件超过此真实类型的大小上限（{actual_limit} 字节）")
        if detected_binary:
            media_type = "application/vnd.microsoft.portable-executable" if detected_binary == "PE" else "application/x-elf"
        spool.seek(0)
        async with db.transaction() as session:
            await require_session(session, session_id, cfg, lock=True)
            await expire_temporary(session, session_id)
            existing = (
                await session.execute(
                    select(AttachmentRow).where(
                        AttachmentRow.session_id == session_id,
                        AttachmentRow.upload_key == upload_key,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                if existing.sha256 != digest.hexdigest() or existing.filename != filename:
                    raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "上传幂等键已用于其他文件")
                if existing.status not in {"temporary", "bound"}:
                    raise ApiError(ErrorCode.CONFLICT, "该上传已移除或过期，请重新选择文件")
                if existing.parse_status != "pending":
                    return metadata(existing, store)
                attachment_id = existing.id
                relative_key = existing.relative_key
            else:
                count = await session.scalar(
                    select(func.count())
                    .select_from(AttachmentRow)
                    .where(AttachmentRow.session_id == session_id)
                )
                if int(count or 0) >= cfg.attachment_max_per_session:
                    raise ApiError(ErrorCode.VALIDATION_FAILED, "该任务已达到附件数量限制")
                # 仅在幂等/配额校验后写 CAS；短事务不跨越上传或文档解析。
                stored = await asyncio.to_thread(
                    store.write_stream,
                    cast(BinaryIO, spool),
                    media_type=media_type,
                    captured_by="user_upload",
                )
                row = AttachmentRow(
                    session_id=session_id,
                    owner_id=cfg.owner_id,
                    upload_key=upload_key,
                    filename=filename,
                    content_type=media_type,
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    relative_key=stored.relative_key,
                    parse_status="pending",
                    expires_at=datetime.now(UTC) + timedelta(hours=cfg.attachment_ttl_hours),
                    metadata_json={},
                )
                session.add(row)
                await session.flush()
                attachment_id = row.id
                relative_key = row.relative_key
    async with _parsers:
        parsed = await parse_attachment(store.resolve(relative_key), extension, cfg)
    if detected_binary:
        parsed["metadata"].update(binary_candidate=True, detected_format=detected_binary,
            note="原件已保存；正文不作为文本解析。可由 Agent 按需调用 binary_profile/binary_strings 进行静态分析；分析状态独立记录。")
    parsed_key = None
    if parsed["status"] in {"ready", "empty"}:
        view = await asyncio.to_thread(
            store.write_bytes,
            json.dumps(parsed, ensure_ascii=False).encode("utf-8"),
            media_type="application/json",
            captured_by="user_upload_extraction",
        )
        parsed_key = view.relative_key
    async with db.transaction() as session:
        row = await session.get(AttachmentRow, attachment_id)
        assert row is not None
        row.parse_status = parsed["status"]
        row.parsed_key = parsed_key
        row.metadata_json = parsed["metadata"]
        return metadata(row, store)


@router.get("", response_model=AttachmentPage)
async def list_attachments(
    session_id: UUID,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    include_temporary: bool = False,
) -> dict[str, Any]:
    authorize(request)
    cfg = get_settings()
    async with get_database().session() as db:
        await require_session(db, session_id, cfg)
        return await list_metadata(
            db,
            session_id,
            cfg,
            ArtifactStore(cfg),
            offset=offset,
            limit=limit,
            include_temporary=include_temporary,
        )


@router.get("/{attachment_id}", response_model=AttachmentRead)
async def get_attachment(session_id: UUID, attachment_id: UUID, request: Request) -> AttachmentRead:
    authorize(request)
    cfg = get_settings()
    async with get_database().session() as db:
        row = await load_attachment(db, session_id, attachment_id, cfg)
        return metadata(row, ArtifactStore(cfg))


@router.get("/{attachment_id}/analyses", response_model=list[BinaryAnalysisState])
async def attachment_analyses(session_id: UUID, attachment_id: UUID, request: Request) -> list[BinaryAnalysisState]:
    authorize(request)
    cfg = get_settings()
    async with get_database().session() as db:
        await load_attachment(db, session_id, attachment_id, cfg)
        rows = (await db.execute(select(AttachmentAnalysisRow).where(
            AttachmentAnalysisRow.attachment_id == attachment_id,
            AttachmentAnalysisRow.session_id == session_id,
            AttachmentAnalysisRow.owner_id == cfg.owner_id,
        ).order_by(AttachmentAnalysisRow.created_at.desc()).limit(8))).scalars().all()
        return [BinaryAnalysisState(analysis_id=str(row.id), kind=row.kind,
            status="interrupted" if row.status == "pending" and row.lease_expires_at and row.lease_expires_at <= datetime.now(UTC) else row.status,
            analyzer_version=row.analyzer_version, error_type=row.error_type,
            summary=row.summary_json) for row in rows]


@router.get("/{attachment_id}/content")
async def download_attachment(
    session_id: UUID, attachment_id: UUID, request: Request, preview: bool = False
) -> FileResponse:
    authorize(request)
    cfg = get_settings()
    store = ArtifactStore(cfg)
    async with get_database().session() as db:
        row = await load_attachment(db, session_id, attachment_id, cfg)
    if not store.exists(row.relative_key):
        raise ApiError(ErrorCode.NOT_FOUND, "附件原文件缺失")
    # 仅验证过的光栅图可内联。HTML/SVG/PDF/源码均下载，不在应用源域执行。
    inline = (
        preview and row.content_type.startswith("image/") and row.parse_status == "metadata_only"
    )
    if preview and not inline:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "此类型仅支持下载或文本读取，不支持内联预览")
    return FileResponse(
        store.resolve(row.relative_key),
        media_type=row.content_type if inline else "application/octet-stream",
        headers={
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(row.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/{attachment_id}/text", response_model=AttachmentSlice)
async def attachment_text(
    session_id: UUID,
    attachment_id: UUID,
    request: Request,
    page: int = Query(1, ge=1),
    start_line: int = Query(1, ge=1),
    end_line: int | None = Query(None, ge=1),
    char_offset: int = Query(0, ge=0),
    max_chars: int = Query(12000, ge=64, le=32000),
) -> AttachmentSlice:
    authorize(request)
    cfg = get_settings()
    async with get_database().session() as db:
        row = await load_attachment(db, session_id, attachment_id, cfg)
    return await asyncio.to_thread(
        read_slice,
        row,
        ArtifactStore(cfg),
        cfg,
        page=page,
        start_line=start_line,
        end_line=end_line,
        char_offset=char_offset,
        max_chars=max_chars,
    )


@router.delete("/{attachment_id}")
async def remove_attachment(
    session_id: UUID, attachment_id: UUID, request: Request
) -> dict[str, Any]:
    authorize(request, write=True)
    cfg = get_settings()
    async with get_database().transaction() as db:
        await require_session(db, session_id, cfg, lock=True)
        row = await load_attachment(db, session_id, attachment_id, cfg)
        if row.source_message_id is not None or row.status == "bound":
            raise ApiError(ErrorCode.CONFLICT, "已绑定历史消息的附件保留，不允许删除")
        row.status = "removed"
    return {"attachment_id": str(attachment_id), "status": "removed", "physical_deleted": False}


@router.post("/cleanup")
async def cleanup_temporary(session_id: UUID, request: Request) -> dict[str, Any]:
    authorize(request, write=True)
    cfg = get_settings()
    async with get_database().transaction() as db:
        await require_session(db, session_id, cfg, lock=True)
        count = await expire_temporary(db, session_id)
    return {
        "expired": count,
        "physical_deleted": False,
        "note": "保守保留共用 CAS 对象；不会删除已绑定文件或其他证据引用。",
    }
