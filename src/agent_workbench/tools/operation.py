"""持久操作身份的解析与复用。

`operation_id` 是"一次逻辑外部操作"的身份（§4.1）。它必须在**图重放**时保持不变，
因此**不能**在工具函数里 `uuid4()`。正确做法：

1. 用 `(run_id, tool_call_id)` 作为稳定键查询 `tool_operations`；
2. 命中且 `request_digest` 相同 -> 复用原 `operation_id`（重放）；
3. 命中但 digest 不同 -> 冲突（模型对同一 tool_call 给了不同参数，说明有 bug）；
4. 未命中 -> 新建一行，生成新的 `operation_id` 并**先持久化**，再发送到 Executor。

显式"再测一次"必须由模型产生**新的 tool_call_id**，从而得到新的逻辑操作 ID，
不会被历史去重误认为重放（§8.5）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import ToolEffectClass
from agent_workbench.persistence.repositories import (
    ToolOperationRepository,
    compute_request_digest,
)
from agent_workbench.persistence.tables import ToolOperationRow


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    operation_id: UUID
    replayed: bool
    row: ToolOperationRow


class OperationRegistry:
    """在调用方提供的**短事务**内准备操作身份。"""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ToolOperationRepository(session)

    async def prepare(
        self,
        *,
        run_id: UUID,
        session_id: UUID,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        effect_class: ToolEffectClass,
        policy_revision: int,
        scope_revision: int,
        dispatch_control_version: int | None = None,
        identity_id: UUID | None = None,
    ) -> PreparedOperation:
        digest = compute_request_digest({"tool": tool_name, "arguments": _canonical(arguments)})

        existing = await self._repo.find_by_tool_call(run_id, tool_call_id)
        if existing is not None:
            if existing.request_digest != digest:
                from agent_workbench.contracts.enums import ErrorCode
                from agent_workbench.contracts.errors import ApiError

                raise ApiError(
                    ErrorCode.OPERATION_CONFLICT,
                    "同一 tool_call_id 的既有操作参数与新参数不一致；拒绝复用",
                    details={
                        "tool_call_id": tool_call_id,
                        "operation_id": str(existing.operation_id),
                    },
                )
            return PreparedOperation(
                operation_id=existing.operation_id, replayed=True, row=existing
            )

        operation_id = uuid.uuid4()
        row, _created = await self._repo.prepare(
            operation_id=operation_id,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            request_digest=digest,
            effect_class=str(effect_class),
            policy_revision=policy_revision,
            scope_revision=scope_revision,
            dispatch_control_version=dispatch_control_version,
            identity_id=identity_id,
        )
        return PreparedOperation(operation_id=row.operation_id, replayed=False, row=row)


def _canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    """去掉只影响展示、不影响副作用的字段（例如模型自填的说明文本）。"""
    return {
        key: value
        for key, value in arguments.items()
        if key not in {"reason", "note", "explanation", "comment"}
    }
