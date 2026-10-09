"""远端工具执行的共享骨架。

`http_request` 与文件工具走的是同一套顺序（§8.5）：

    prepare tool_operation -> 提交控制端记录 -> Executor 幂等接收
    -> 等待/接受 -> 记录结果 -> 写 tool message/checkpoint

把它抽出来是为了避免两套实现漂移 —— 尤其是 `operation_id` 的复用语义，
一旦某条路径自己随机生成 ID，幂等保证就破了。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent_workbench.contracts.enums import (
    ErrorCode,
    ExecutionCertainty,
    OperationStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.tool import ArtifactRef, ToolError, ToolResult
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
)
from agent_workbench.persistence.repositories import (
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.runtime import Toolbox


async def prepare_operation(
    toolbox: Toolbox,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    effect_class: ToolEffectClass,
) -> tuple[UUID, bool]:
    """在一个**短事务**内准备操作身份。禁止在事务里等待网络或模型。"""
    async with toolbox.db.transaction() as db:
        prepared = await OperationRegistry(db).prepare(
            run_id=toolbox.run_id,
            session_id=toolbox.session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            effect_class=effect_class,
            policy_revision=toolbox.policy_revision,
            scope_revision=toolbox.scope_revision,
            identity_id=toolbox.identity_id,
        )
        return prepared.operation_id, prepared.replayed


async def record_submitted(toolbox: Toolbox, operation_id: UUID, job_id: UUID) -> None:
    async with toolbox.db.transaction() as db:
        await ToolOperationRepository(db).attach_job(operation_id, job_id, status="running")
        await ToolJobRepository(db).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id=toolbox.settings.executor_id,
            state="received",
        )


async def record_result(
    toolbox: Toolbox,
    *,
    operation_id: UUID,
    job_id: UUID | None,
    status: str,
    refs: list[ArtifactRef] | None = None,
    failure: str | None = None,
) -> None:
    from agent_workbench.contracts.events import EventType
    from agent_workbench.observability.events import append_event

    async with toolbox.db.transaction() as db:
        await ToolOperationRepository(db).mark_result(
            operation_id, status=status, artifact_ids=[str(r.artifact_id) for r in (refs or [])]
        )
        if job_id is not None:
            await ToolJobRepository(db).upsert(
                job_id=job_id,
                operation_id=operation_id,
                executor_id=toolbox.settings.executor_id,
                state=status,
                sync_status="in_sync",
            )
        if failure is not None:
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.ERROR,
                payload={
                    "scope": "tool_submit",
                    "operation_id": str(operation_id),
                    "message": failure,
                    "execution_certainty": str(ExecutionCertainty.NOT_APPLIED),
                },
            )


def rejection_result(operation_id: UUID, exc: ExecutorRejected) -> ToolResult:
    """执行岛**明确拒绝** -> 操作确定没有执行，重试同样会被拒。"""
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.FAILED,
        summary=f"执行岛明确拒绝了该操作，请求未发出：{exc}",
        error=ToolError(
            code=str(ErrorCode.VALIDATION_FAILED),
            message=str(exc),
            retryable=False,
            recovery_hint="这属于参数/策略不匹配，重试同样会被拒绝；请修正参数。",
        ),
        execution_certainty=ExecutionCertainty.NOT_APPLIED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


def transport_unknown_result(operation_id: UUID, exc: Exception) -> ToolResult:
    """传输不确定 -> 可能已经执行，必须先查询再决定。"""
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.UNKNOWN,
        summary="无法确认远端是否已接收该操作；必须先查询再决定后续，不要直接重试",
        error=ToolError(
            code=str(ErrorCode.EXECUTOR_UNAVAILABLE),
            message=str(exc),
            retryable=True,
            recovery_hint="按同一 operation_id 重新提交只会得到原 job，不会产生第二个副作用。",
        ),
        execution_certainty=ExecutionCertainty.UNCERTAIN,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


async def wait_or_accept(
    toolbox: Toolbox, operation_id: UUID, job_id: UUID, *, accept_note: str
) -> ToolResult | None:
    """在交互阈值内等待；超时返回 `accepted`，让模型调用链不被占住（§8.3）。

    返回 `None` 表示已经到达终态，调用方继续后续处理。
    """
    try:
        await asyncio.wait_for(
            toolbox.wait_for_terminal(job_id), timeout=toolbox.interactive_threshold_seconds
        )
    except TimeoutError:
        return ToolResult.accepted(operation_id, job_id, accept_note)
    return None


async def download_result_json(toolbox: Toolbox, job_id: UUID) -> dict[str, Any] | None:
    """取回 Executor 写在 job 目录里的 `result.json`。

    返回 `None` 表示拿不到（工件缺失或传输失败）——调用方必须按"结果不可用"处理，
    **不能**把缺失当成成功。
    """
    try:
        data, _headers = await toolbox.executor.download_artifact(
            job_id, "result.json", limit=2 * 1024 * 1024
        )
    except (ExecutorConnectionError, ExecutorProtocolError):
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def json_text(result: ToolResult) -> str:
    """工具进入模型上下文的形态：统一结果契约的 JSON 文本。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
