"""任务终止时关闭保留资源；暂停不调用这里。实际状态仍以执行端回执为准。"""

from __future__ import annotations

from typing import Any
from uuid import uuid5

from agent_workbench.contracts.execution_runtime import execution_request_digest
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import (
    ExecutionDelegationRepository,
    ExecutionResourceRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import RunRow


async def terminate_session_resources(
    db: Database, client: Any, run: RunRow,
) -> bool:
    async with db.transaction() as session:
        duties = list(await ExecutionDelegationRepository(session).active_for_session(run.session_id))
        for duty in duties:
            await ExecutionDelegationRepository(session).cancel(duty.id, reason="用户终止任务")
        from agent_workbench.domain.execution_delegations import revocation_payload
        revocations = await revocation_payload(session, run.session_id)
        resources = list(await ExecutionResourceRepository(session).active_for_session(run.session_id))
        current = await session.get(RunRow, run.id)
        assert current is not None
        generation, version = int(current.owner_generation), int(current.control_version)
    if not resources:
        return True
    ack = await client.execution_sync_control({
        "session_id": str(run.session_id), "run_id": str(run.id), "owner": run.owner or "",
        "owner_generation": generation, "control_version": version, "expected_seq": None,
        "revocations": revocations,
    })
    if not ack.get("applied"):
        return False
    confirmed = True
    for resource in resources:
        action = "terminal_close" if resource.kind == "terminal" else "cancel_resource"
        arguments = ({"terminal_id": str(resource.resource_id), "escalate": True}
                     if resource.kind == "terminal" else {"resource_id": str(resource.resource_id)})
        operation_id = uuid5(run.id, f"terminate:{resource.resource_id}")
        digest = execution_request_digest(action, arguments)
        async with db.transaction() as session:
            await ToolOperationRepository(session).prepare(
                operation_id=operation_id, run_id=run.id, session_id=run.session_id,
                tool_call_id=f"terminate:{resource.resource_id}", tool_name="terminate_resource",
                request_digest=digest, effect_class="side_effecting", policy_revision=1,
                scope_revision=1, dispatch_control_version=version,
            )
        receipt = await client.execution_submit({
            "operation_id": str(operation_id), "session_id": str(run.session_id),
            "run_id": str(run.id), "tool_call_id": f"terminate:{resource.resource_id}",
            "request_digest": digest, "action": action, "arguments": arguments,
            "owner_generation": generation, "dispatch_control_version": version,
            "session_control_seq": ack["fence"]["seq"], "wait_ms": 20000,
        })
        stopped = receipt.get("process_confirmed_exited") is True
        confirmed &= stopped
        async with db.transaction() as session:
            await ToolOperationRepository(session).mark_result(
                operation_id, status="succeeded" if stopped else "unknown")
            await ExecutionResourceRepository(session).apply_close_result(
                resource.resource_id, confirmed=stopped,
                state=str(receipt.get("process_state") or "closed") if stopped else "unknown",
                detail={"operation_id": str(operation_id)},
            )
    return confirmed
