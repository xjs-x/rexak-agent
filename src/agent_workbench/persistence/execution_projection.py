"""从执行资源核对结果同步操作与 job 投影，不推断目标测试结论。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.persistence.tables import ExecutionResourceRow, ToolJobRow, ToolOperationRow


def execution_operation_status(state: str, exit_code: int | None, reason: str | None) -> str:
    if state in {"exited", "closed"}:
        if reason == "user_cancelled":
            return "cancelled"
        if exit_code is None:
            return "unknown"
        return "succeeded" if exit_code == 0 else "failed"
    return "unknown" if state == "unknown" else "running"


async def sync_resource_operation(session: AsyncSession, resource: ExecutionResourceRow) -> None:
    """只同步资源的原始启动操作，不把后续读取/输入操作当作整个进程。"""
    if resource.origin_operation_id is None or resource.sync_status != "in_sync":
        return
    operation = (
        await session.execute(
            select(ToolOperationRow)
            .where(
                ToolOperationRow.operation_id == resource.origin_operation_id,
                ToolOperationRow.session_id == resource.session_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if operation is None:
        return
    terminal = resource.state in {"exited", "closed"}
    if operation.status in {"succeeded", "failed", "cancelled"} and not terminal:
        return
    status = execution_operation_status(
        resource.state, resource.exit_code, resource.termination_reason
    )
    operation.status = status
    operation.updated_at = datetime.now(UTC)
    if terminal:
        operation.result_snapshot = {
            "status": status,
            "data": {
                "resource_id": str(resource.resource_id),
                "process_state": resource.state,
                "exit_code": resource.exit_code,
                "termination_reason": resource.termination_reason,
            },
        }
    job = await session.get(ToolJobRow, resource.resource_id)
    if job is not None and job.operation_id == operation.operation_id:
        job.state = status if terminal else resource.state
        job.sync_status = "in_sync"
        job.last_synced_at = resource.last_checked_at
