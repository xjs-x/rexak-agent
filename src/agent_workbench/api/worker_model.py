"""Private loopback model delivery to the trusted Pi runtime, outside task artifacts."""

import hmac
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_workbench.config.settings import get_settings
from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.config.worker_model import shared_worker_model
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.worker_evidence import guard
from agent_workbench.models.provider import ProviderConfigurationError
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import AgentDelegationRow, RunRow, SessionRow

router = APIRouter()


@router.get("/api/internal/worker-model", include_in_schema=False)
async def worker_model(request: Request, delegation_id: UUID | None = None):
    settings = get_settings()
    secret = settings.ultra_worker_token or read_worker_config(settings).get("token", "")
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or len(secret) < 24 or not hmac.compare_digest(bearer, secret)):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker 模型配置凭据无效")
    snapshot = None
    if delegation_id is not None:
        async with get_database().session() as db:
            row = await db.get(AgentDelegationRow, delegation_id)
            run = await db.get(RunRow, row.parent_run_id) if row else None
            parent = await db.get(SessionRow, row.parent_session_id) if row else None
            guard(parent, run, row, settings)
            assert run
            snapshot = (run.config_snapshot or {}).get("model_binding")
            if not snapshot:
                raise ApiError(ErrorCode.CONFLICT, "主智能体模型绑定不可用")
    try:
        config = shared_worker_model(settings, snapshot)
    except (ValueError, ProviderConfigurationError, OSError):
        # Never echo provider errors or secret-bearing configuration values.
        raise ApiError(ErrorCode.CONFLICT, "请在模型设置中配置有效的模型和 Key") from None
    return JSONResponse(config.transport_value(), headers={"Cache-Control": "no-store"})
