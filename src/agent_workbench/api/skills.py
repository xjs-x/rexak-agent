"""Skill 管理接口；复用本机会话、Origin/CSRF，不在 API 中运行 Agent。"""

from __future__ import annotations

import base64
import binascii
import zipfile
from typing import Any
from uuid import UUID

import yaml
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.skill_package import decode_package, encode_package
from agent_workbench.contracts.skills import (
    SkillCreate,
    SkillFilesEdit,
    SkillImport,
    SkillList,
    SkillRead,
    SkillResource,
    SkillSessionRead,
    SkillUpdate,
)
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import (
    ArtifactRow,
    ProjectRow,
    SessionRow,
    SkillInstallationRow,
    SkillVersionRow,
)
from agent_workbench.skills.parser import parse_skill
from agent_workbench.skills.service import SkillAlreadyInstalled, SkillService, SkillVersionConflict


class SkillRoute(APIRoute):
    def get_route_handler(self):  # type: ignore[no-untyped-def]
        handler = super().get_route_handler()

        async def wrapped(request: Request):  # type: ignore[no-untyped-def]
            try:
                return await handler(request)
            except (SkillAlreadyInstalled, SkillVersionConflict) as exc:
                raise ApiError(ErrorCode.CONFLICT, str(exc)) from exc
            except (ValueError, OSError, yaml.YAMLError, zipfile.BadZipFile) as exc:
                raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
        return wrapped


router = APIRouter(prefix="/api", tags=["skills"], route_class=SkillRoute)


def guard(request: Request, *, write: bool = False) -> SkillService:
    from agent_workbench.api.routes import get_auth

    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    if write:
        auth.require_csrf(request, ticket)
    return SkillService(get_database())


@router.get("/skills", response_model=SkillList)
async def list_skills(request: Request, query: str = "", offset: int = Query(0, ge=0),
                      limit: int = Query(50, ge=1, le=200)) -> SkillList:
    service = guard(request)
    rows = await service.list()
    rows = [row for row in rows if query.casefold() in f"{row.name} {row.description}".casefold()]
    return SkillList(skills=rows[offset:offset + limit], total=len(rows),
        next_cursor=str(offset + limit) if offset + limit < len(rows) else None, root=str(service.root))


@router.post("/skills/rescan", response_model=SkillList)
async def rescan(request: Request) -> SkillList:
    service = guard(request, write=True)
    rows, errors = await service.rescan()
    return SkillList(skills=rows, total=len(rows), errors=errors, root=str(service.root))


@router.post("/skills/import", response_model=SkillRead)
async def import_skill(request: Request, payload: SkillImport) -> SkillRead:
    service = guard(request, write=True)
    if bool(payload.directory) == bool(payload.zip_base64):
        raise ApiError(ErrorCode.VALIDATION_FAILED, "directory 与 zip_base64 必须二选一")
    try:
        if payload.directory:
            return await service.import_directory(payload.directory)
        files = decode_package(base64.b64decode(payload.zip_base64 or "", validate=True), allow_root=True)
        spec = parse_skill(files)
        return await service.publish(files, f"zip:{spec['name']}")
    except (ValueError, OSError, yaml.YAMLError, zipfile.BadZipFile, binascii.Error) as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc


@router.post("/skills/create", response_model=SkillRead)
async def create_skill(request: Request, payload: SkillCreate) -> SkillRead:
    service = guard(request, write=True)
    header = "---\n" + yaml.safe_dump({"name": payload.name, "description": payload.description},
                                        allow_unicode=True, sort_keys=False) + "---\n"
    return await service.publish({"SKILL.md": (header + payload.body + "\n").encode("utf-8")},
                                 f"user:web:{payload.name}", create_only=True)


@router.put("/skills/{skill_id}/files", response_model=SkillRead)
async def edit_skill_files(request: Request, skill_id: UUID, payload: SkillFilesEdit) -> SkillRead:
    service = guard(request, write=True)
    binaries = {path: base64.b64decode(value, validate=True) for path, value in payload.binary_files.items()}
    return await service.edit_files(str(skill_id), payload.text_files, binaries,
                                    base_version_id=payload.base_version_id)


@router.get("/skills/{skill_id}/versions", response_model=SkillList)
async def versions(request: Request, skill_id: UUID) -> SkillList:
    rows = await guard(request).versions(str(skill_id))
    return SkillList(skills=rows, total=len(rows))


@router.get("/skills/{skill_id}", response_model=SkillRead)
async def detail(request: Request, skill_id: UUID) -> SkillRead:
    card, _files, _spec = await guard(request).files(str(skill_id), enforce_enabled=False)
    return card


@router.patch("/skills/{skill_id}", response_model=SkillList)
async def update_skill(request: Request, skill_id: UUID, payload: SkillUpdate) -> SkillList:
    service = guard(request, write=True)
    values = payload.model_dump(exclude_none=True)
    ids = {*values.get("project_ids", []), *values.get("default_for_projects", [])}
    async with service.db.session() as db:
        for value in ids:
            row = await db.get(ProjectRow, UUID(value))
            if row is None or row.owner_id != service.settings.owner_id:
                raise ApiError(ErrorCode.NOT_FOUND, "绑定的项目不存在")
    await service.update(str(skill_id), values)
    rows = await service.versions(str(skill_id))
    return SkillList(skills=rows, total=len(rows))


@router.delete("/skills/{skill_id}")
async def archive_skill(request: Request, skill_id: UUID) -> dict:
    await guard(request, write=True).archive(str(skill_id))
    return {"archived": True, "skill_id": str(skill_id)}


@router.get("/skills/{skill_id}/versions/{version_id}/files", response_model=SkillResource)
async def resource(request: Request, skill_id: UUID, version_id: UUID, path: str = "SKILL.md",
                   offset: int = Query(0, ge=0), limit: int = Query(8000, ge=1, le=16000),
                   files_offset: int = Query(0, ge=0)) -> SkillResource:
    service = guard(request)
    _card, files, _spec = await service.files(str(skill_id), str(version_id), enforce_enabled=False)
    return service.resource(files, path, offset, limit, files_offset)


@router.get("/skills/{skill_id}/versions/{version_id}/export")
async def export_skill(request: Request, skill_id: UUID, version_id: UUID) -> Response:
    service = guard(request)
    _card, files, _spec = await service.files(str(skill_id), str(version_id), enforce_enabled=False)
    return Response(encode_package(files), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="skill-{skill_id}.zip"'})


@router.get("/sessions/{session_id}/skills", response_model=SkillSessionRead)
async def session_skills(request: Request, session_id: UUID) -> SkillSessionRead:
    service = guard(request)
    async with service.db.session() as db:
        row = (await db.execute(select(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.status != "deleted",
                   SessionRow.owner_id == service.settings.owner_id))).scalar_one_or_none()
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, "任务不存在")
        thread = row.thread_id
    async with open_checkpointer_connection(get_settings()) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple({"configurable": {"thread_id": thread}})
    values: dict[str, Any] = saved.checkpoint.get("channel_values", {}) if saved else {}
    refs = {key: ref for key, ref in (values.get("active_skills") or {}).items() if ref}
    legacy = await service.legacy_ids(refs)
    active = []
    async with service.db.session() as db:
        for key, ref in refs.items():
            if key in legacy:
                continue
            installed = await db.get(SkillInstallationRow, UUID(key))
            if installed is None or installed.owner_id != service.settings.owner_id:
                continue
            try:
                version_id = UUID(str(ref.get("version_id")))
            except ValueError:
                version_id = None
            version = await db.get(SkillVersionRow, version_id) if version_id else None
            valid_version = (version is not None and version.installation_id == installed.id
                             and version.package_hash == ref.get("package_hash"))
            artifact = await db.get(ArtifactRow, version.artifact_id) if version and valid_version else None
            if not installed.enabled:
                reason = "技能已停用"
            elif not artifact or artifact.availability != "available" or not service.store.exists(artifact.relative_key):
                reason = "固定版本的资源不可用"
            else:
                reason = ""
            active.append({**ref, "name": version.spec["name"] if version and valid_version else installed.name,
                           "status": "unavailable" if reason else "loaded", "reason": reason})
    return SkillSessionRead(active=active)


@router.get("/sessions/{session_id}/skill-catalog", response_model=SkillList)
async def session_skill_catalog(request: Request, session_id: UUID) -> SkillList:
    service = guard(request)
    from agent_workbench.domain.run_state import find_active_run

    async with service.db.session() as db:
        session = await db.get(SessionRow, session_id)
        if session is None or session.owner_id != service.settings.owner_id or session.status == "deleted":
            raise ApiError(ErrorCode.NOT_FOUND, "任务不存在")
        run = await find_active_run(db, session_id)
    if run is None:
        rows = [item for item in await service.list() if item.enabled]
    else:
        rows = [SkillRead.model_validate(item) for item in await service.catalog(run.id)
                if item.get("enabled")]
    return SkillList(skills=rows, total=len(rows))
