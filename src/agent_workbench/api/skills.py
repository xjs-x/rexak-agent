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
    SkillImport,
    SkillList,
    SkillRead,
    SkillResource,
    SkillSessionRead,
    SkillUpdate,
)
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import ProjectRow, SessionRow
from agent_workbench.skills.parser import parse_skill
from agent_workbench.skills.service import SkillService


class SkillRoute(APIRoute):
    def get_route_handler(self):  # type: ignore[no-untyped-def]
        handler = super().get_route_handler()

        async def wrapped(request: Request):  # type: ignore[no-untyped-def]
            try:
                return await handler(request)
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
        row = (await db.execute(select(SessionRow).join(ProjectRow, SessionRow.project_id == ProjectRow.id)
            .where(SessionRow.id == session_id, SessionRow.status != "deleted",
                   ProjectRow.owner_id == service.settings.owner_id))).scalar_one_or_none()
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, "任务不存在")
        thread = row.thread_id
    async with open_checkpointer_connection(get_settings()) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple({"configurable": {"thread_id": thread}})
    values: dict[str, Any] = saved.checkpoint.get("channel_values", {}) if saved else {}
    refs = {key: ref for key, ref in (values.get("active_skills") or {}).items() if ref}
    legacy = await service.legacy_ids(refs)
    return SkillSessionRead(active=[ref for key, ref in refs.items() if key not in legacy])
