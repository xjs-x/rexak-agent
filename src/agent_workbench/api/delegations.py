"""本地控制台读取子任务投影；浏览器不直接连接 A2A。"""

from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.delegation import DelegationList, DelegationRead
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import AgentDelegationRow, SessionRow

router = APIRouter()


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
