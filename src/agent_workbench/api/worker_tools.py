"""Authenticated task-bound Pi Operations callback."""

from uuid import UUID

from fastapi import APIRouter, Request

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.worker_tools import WorkerToolRequest
from agent_workbench.domain.worker_tools import WorkerToolService
from agent_workbench.persistence.db import get_database

router = APIRouter()


@router.post("/api/internal/delegations/{delegation_id}/tools", include_in_schema=False)
async def worker_tools(delegation_id: UUID, value: WorkerToolRequest, request: Request):
    return await WorkerToolService(get_database(), get_settings()).handle(
        delegation_id, value, request.headers.get("Authorization", "").removeprefix("Bearer "))
