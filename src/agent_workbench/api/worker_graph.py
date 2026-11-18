"""Internal Worker read-only graph endpoint; no Session selector in model input."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.graph_read import WorkerGraphRequest
from agent_workbench.domain.worker_graph import WorkerGraphService
from agent_workbench.persistence.db import get_database

router = APIRouter()


@router.post("/internal/delegations/{delegation_id}/graph", include_in_schema=False)
async def worker_graph(delegation_id: UUID, body: WorkerGraphRequest, request: Request):
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "localhost"}:
        from agent_workbench.contracts.enums import ErrorCode
        from agent_workbench.contracts.errors import ApiError

        raise ApiError(ErrorCode.FORBIDDEN, "图通道仅允许本机 Worker")
    result = await WorkerGraphService(get_database(), get_settings()).handle(
        delegation_id, body, request.headers.get("authorization", "").removeprefix("Bearer "))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
