"""Private authenticated runtime callback; no browser session/CSRF bypass for ordinary APIs."""

from uuid import UUID

from fastapi import APIRouter, Request

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.worker_evidence import WorkerEvidenceRequest
from agent_workbench.domain.worker_evidence import WorkerEvidenceService
from agent_workbench.persistence.db import get_database

router = APIRouter()


@router.post("/api/internal/delegations/{delegation_id}/evidence", include_in_schema=False)
async def worker_evidence(delegation_id: UUID, value: WorkerEvidenceRequest, request: Request):
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return await WorkerEvidenceService(get_database(), get_settings()).handle(delegation_id, value, bearer)
