"""附件交付的单一编排入口，Main 与委派输入复用。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.execution_runtime import InputFileArguments
from agent_workbench.domain.attachments import load_attachment
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
)
from agent_workbench.persistence.repositories import ToolOperationRepository
from agent_workbench.persistence.tables import AgentDelegationRow
from agent_workbench.tools.execution_dispatch import (
    build_envelope,
    load_capabilities,
    prepare_execution_operation,
    require_capability,
    sync_fence,
)
from agent_workbench.tools.runtime import Toolbox


async def deliver_attachment(box: Toolbox, attachment_id: UUID, tool_call_id: str,
                             *, worker_id: UUID | None = None) -> dict:
    if not tool_call_id:
        raise ValueError("附件交付必须具有稳定 ToolCall 身份")
    await box.assert_control_current()
    require_capability(await load_capabilities(box), "input_files")
    async with box.db.session() as db:
        if worker_id:
            worker = await db.get(AgentDelegationRow, worker_id)
            if (worker is None or worker.parent_session_id != box.session_id
                    or worker.parent_run_id != box.run_id or worker.cancel_requested
                    or worker.state != "submitting" or not worker.authorization_snapshot.get("executor_access")
                    or str(attachment_id) not in (worker.handoff_snapshot or {}).get("attachment_ids", [])
                    or worker.authorization_snapshot.get("owner_generation") != box.owner_generation
                    or worker.authorization_snapshot.get("control_version") != box.control_version):
                raise ApiError(ErrorCode.FORBIDDEN, "Worker 未获附件交付授权或授权已失效")
        row = await load_attachment(db, box.session_id, attachment_id, box.settings, bound_only=True)
    value = InputFileArguments(attachment_id=row.id, filename=row.filename, sha256=row.sha256,
                              byte_size=row.byte_size, worker_workspace_id=worker_id)
    path = box.artifacts.resolve(row.relative_key)
    if (not path.is_file() or path.stat().st_size != value.byte_size
            or not await asyncio.to_thread(box.artifacts.verify, row.relative_key, row.sha256)):
        raise ValueError("附件原件缺失或完整性校验失败")
    arguments = value.model_dump(mode="json")
    operation_id, prepared, replayed = await prepare_execution_operation(
        box, tool_call_id=tool_call_id, tool_name="materialize_attachment",
        action="materialize_attachment", arguments=arguments,
    )
    if replayed:
        try:
            observed = await box.executor.execution_operation(str(operation_id))
        except (ExecutorRejected, ExecutorConnectionError, ExecutorProtocolError) as exc:
            raise ExecutorConnectionError(f"附件操作 {operation_id} 待核对，不自动重发") from exc
        receipt = observed.get("receipt") or {}
        source = receipt.get("input_file") or {}
        if (observed.get("status") != "succeeded" or receipt.get("operation_id") != str(operation_id)
                or source.get("attachment_id") != str(attachment_id) or source.get("sha256") != row.sha256
                or source.get("session_id") != str(box.session_id)
                or source.get("worker_workspace_id") != (str(worker_id) if worker_id else None)):
            raise ExecutorConnectionError(f"附件操作 {operation_id} 尚无成功回执，不自动重发")
        async with box.db.transaction() as db:
            await ToolOperationRepository(db).mark_result(operation_id, status="succeeded")
        return {**receipt, "deduplicated": True}
    await sync_fence(box)
    await box.assert_control_current()
    envelope = build_envelope(box, operation_id=operation_id, tool_call_id=tool_call_id,
        action="materialize_attachment", arguments=arguments, request_digest=prepared["digest"],
        delegation_id=str(worker_id) if worker_id else None,
        delegation_revision=1 if worker_id else None)
    try:
        receipt = await box.executor.execution_input_file(envelope, path)
    except ExecutorRejected:
        async with box.db.transaction() as db:
            await ToolOperationRepository(db).mark_result(operation_id, status="failed")
        raise
    except (ExecutorConnectionError, ExecutorProtocolError, asyncio.CancelledError) as exc:
        async with box.db.transaction() as db:
            await ToolOperationRepository(db).mark_result(operation_id, status="unknown")
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ExecutorConnectionError(f"附件操作 {operation_id} 结果不确定，需按原身份核对：{exc}") from exc
    async with box.db.transaction() as db:
        await ToolOperationRepository(db).mark_result(operation_id, status="succeeded")
    return receipt
