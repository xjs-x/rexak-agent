"""官方图模型调用的接缝；单次调用观测与结算交给公共模型层。"""

from __future__ import annotations

import hashlib
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.response_recovery import recovery_scope
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import PriceCatalog, default_price_catalog
from agent_workbench.models.invocation import InvocationScope, ModelRequestTimeout, invoke_attempt


def logical_call_id_for(
    *, run_id: UUID, segment_index: int, message_count: int, system_prompt_digest: str
) -> UUID:
    raw = f"{run_id}|{segment_index}|{message_count}|{system_prompt_digest}".encode()
    return UUID(bytes=hashlib.sha256(raw).digest()[:16])


class ModelObserverMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(
        self,
        price_catalog: PriceCatalog | None = None,
        *,
        request_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__()
        self._catalog = price_catalog or default_price_catalog()
        self._request_timeout_seconds = request_timeout_seconds
        self._idle_timeout_seconds = idle_timeout_seconds

    async def awrap_model_call(self, request, handler):
        context = getattr(request.runtime, "context", None)
        if context is None:
            return await handler(request)
        state = request.state or {}
        # 恢复周期内复用同一 logical_call_id：同一待执行模型决策的多次网络尝试
        # 共用逻辑身份，重试次数、用量、成本与截断原因分别记录（§3.4）。
        scope = recovery_scope.get()
        if scope is not None:
            logical_id = scope.logical_call_id
        else:
            digest = hashlib.sha256(
                str(getattr(request.system_message, "content", "") or "").encode("utf-8")
            ).hexdigest()[:16]
            logical_id = logical_call_id_for(
                run_id=context.toolbox.run_id,
                segment_index=int(state.get("segment_index") or 0),
                message_count=len(request.messages),
                system_prompt_digest=digest,
            )
        return await invoke_attempt(
            lambda: handler(request),
            scope=InvocationScope(context.toolbox, context.model_stream),
            logical_id=logical_id,
            phase="agent",
            catalog=self._catalog,
            request_timeout_seconds=self._request_timeout_seconds,
            idle_timeout_seconds=self._idle_timeout_seconds,
            manifest=getattr(context.toolbox, "context_manifest", None),
            require_recorded=scope is not None,
        )


__all__ = ["ModelObserverMiddleware", "ModelRequestTimeout", "logical_call_id_for"]
