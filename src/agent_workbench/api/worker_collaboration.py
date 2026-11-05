"""Private task-bound callback, outside browser API authentication."""

from uuid import UUID

from fastapi import APIRouter, Request

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.collaboration import WorkerFindingRequest
from agent_workbench.domain.collaboration import CollaborationService
from agent_workbench.persistence.db import get_database

router = APIRouter()


@router.post("/api/internal/delegations/{delegation_id}/findings", include_in_schema=False)
async def worker_finding(delegation_id: UUID, value: WorkerFindingRequest, request: Request):
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return await CollaborationService(get_database(), get_settings()).report(delegation_id, value, bearer)


@router.post("/api/internal/delegations/{delegation_id}/messages/{message_id}/admit", include_in_schema=False)
async def admit_message(delegation_id: UUID, message_id: UUID, value: WorkerFindingRequest, request: Request):
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return await CollaborationService(get_database(), get_settings()).admit_message(delegation_id, message_id, value, bearer)
