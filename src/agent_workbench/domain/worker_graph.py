"""Read-only graph callback using the existing task-bound control channel guards."""

import hashlib
import hmac
from uuid import UUID

from sqlalchemy import select

from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.graph_read import WorkerGraphRequest
from agent_workbench.domain.graph_read import read_shared
from agent_workbench.domain.worker_evidence import guard
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.tables import AgentDelegationRow, RunRow, SessionRow


def graph_signature(secret: str, delegation_id: UUID, value: WorkerGraphRequest) -> str:
    identity = f"graph:v1:{delegation_id}:{value.task_id}:{value.worker_id}:{value.context_id}"
    return hmac.new(secret.encode(), identity.encode(), hashlib.sha256).hexdigest()


class WorkerGraphService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def handle(self, delegation_id: UUID, value: WorkerGraphRequest, bearer: str) -> dict:
        secret = self.settings.ultra_worker_token or read_worker_config(self.settings).get("token", "")
        if len(secret) < 24 or not hmac.compare_digest(bearer, graph_signature(secret, delegation_id, value)):
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker 图读取凭据无效")
        async with A2AWorkerClient(self.settings) as client:
            remote = await client.lookup(delegation_id)
        if (not remote or remote.get("phase") != "running" or remote.get("cancel_requested")
                or remote.get("task_id") != value.task_id or remote.get("worker_id") != str(value.worker_id)
                or (remote.get("task") or {}).get("contextId") != str(value.context_id)):
            raise ApiError(ErrorCode.CONFLICT, "远端委派身份或执行状态不允许读图")
        async with self.db.transaction() as db:
            initial = await db.get(AgentDelegationRow, delegation_id)
            if not initial:
                raise ApiError(ErrorCode.NOT_FOUND, "委派不存在")
            # Same lock order as evidence/control; no write, receipt or maintenance cursor updates.
            parent = await db.scalar(select(SessionRow).where(SessionRow.id == initial.parent_session_id).with_for_update())
            run = await db.scalar(select(RunRow).where(RunRow.id == initial.parent_run_id).with_for_update())
            row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update().execution_options(populate_existing=True))
            guard(parent, run, row, self.settings)
            assert row and parent
            tool = {"query": "graph_query", "overview": "graph_overview", "read": "read_shared_graph"}[value.action]
            if (row.handoff_snapshot.get("graph_access") != "session_graph_read_v1"
                    or tool not in row.authorization_snapshot.get("tool_allowlist", [])):
                raise ApiError(ErrorCode.FORBIDDEN, "委派没有该只读图权限")
            if (row.context_id != value.context_id or (row.a2a_task_id and row.a2a_task_id != value.task_id)
                    or (row.worker_id and row.worker_id != str(value.worker_id))):
                raise ApiError(ErrorCode.CONFLICT, "本地委派身份不匹配")
            result = await read_shared(db, parent.id, value, self.settings)
            guard(parent, run, row, self.settings)
            return result
