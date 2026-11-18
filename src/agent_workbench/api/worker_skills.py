"""Task-bound Pi Skill reads; the Python SkillService remains the package authority."""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.config.settings import get_settings
from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.worker_evidence import guard
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import AgentDelegationRow, RunRow, SessionRow
from agent_workbench.skills.parser import parse_skill
from agent_workbench.skills.service import SkillService

router = APIRouter()


class WorkerSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=128)
    worker_id: UUID
    context_id: UUID
    action: Literal["catalog", "load", "resource"]
    skill_id: UUID | None = None
    version_id: UUID | None = None
    path: str | None = None
    offset: int = Field(default=0, ge=0)


@router.post("/api/internal/delegations/{delegation_id}/skills", include_in_schema=False)
async def worker_skills(delegation_id: UUID, value: WorkerSkillRequest, request: Request) -> dict:
    settings = get_settings()
    secret = settings.ultra_worker_token or read_worker_config(settings).get("token", "")
    identity = f"skills:v1:{delegation_id}:{value.task_id}:{value.worker_id}:{value.context_id}"
    expected = hmac.new(secret.encode(), identity.encode(), hashlib.sha256).hexdigest()
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if len(secret) < 24 or not hmac.compare_digest(expected, bearer):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker Skill 凭据无效")
    async with A2AWorkerClient(settings) as client:
        remote = await client.lookup(delegation_id)
    if (not remote or remote.get("phase") != "running" or remote.get("cancel_requested")
            or remote.get("task_id") != value.task_id or remote.get("worker_id") != str(value.worker_id)
            or (remote.get("task") or {}).get("contextId") != str(value.context_id)):
        raise ApiError(ErrorCode.CONFLICT, "Worker Skill 身份或状态不匹配")
    database = get_database()
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, delegation_id)
        parent = await db.get(SessionRow, row.parent_session_id) if row else None
        run = await db.get(RunRow, row.parent_run_id) if row else None
        guard(parent, run, row, settings)
        assert row
        if row.context_id != value.context_id or (row.a2a_task_id and row.a2a_task_id != value.task_id):
            raise ApiError(ErrorCode.CONFLICT, "Worker Skill 委派映射不匹配")
        assert parent and run
        session_id, run_id = parent.id, run.id
    service = SkillService(database, settings)
    catalog = await service.catalog(run_id)
    if value.action == "catalog":
        return {"skills": [{key: item[key] for key in ("skill_id", "name", "description", "version_id", "package_hash")}
                           for item in catalog], "total": len(catalog)}
    if value.skill_id is None or value.version_id is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "Skill 身份与版本必填")
    item = next((item for item in catalog if item["skill_id"] == str(value.skill_id)), None)
    if not item or item["version_id"] != str(value.version_id):
        raise ApiError(ErrorCode.FORBIDDEN, "Skill 不在本 Run 固定目录")
    card, files, _spec = await service.session_files(session_id, str(value.skill_id), str(value.version_id))
    if card.package_hash != item["package_hash"]:
        raise ApiError(ErrorCode.CONFLICT, "Skill 包哈希不匹配")
    if value.action == "load":
        return {"skill_id": card.skill_id, "version_id": card.version_id,
                "package_hash": card.package_hash, "name": card.name,
                "body": parse_skill(files)["body"],
                "files": sorted(files)}
    if value.path is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "资源路径必填")
    return service.resource(files, value.path, value.offset, 16000).model_dump()
