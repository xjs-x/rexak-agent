"""控制端执行操作的共享派发骨架（`/execution/v1`）。

顺序（与既有 HTTP/文件工具一致，§8.5）：
1. 能力预检（真实能力可用且配置可信才继续；**不回退到控制端执行命令**）；
2. `assert_control_current`（过期模型响应不得执行）；
3. 持久操作身份（`OperationRegistry`，重放复用 operation_id）；
4. 同步受信会话 fence（执行端只接受匹配当前 fence 的请求）；
5. 提交操作包；传输不确定时按 operation_id 核对，**不重复提交**；
6. 把观察到的资源状态写入控制端只读投影（真实状态仍归执行端台账）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent_workbench.contracts.enums import ErrorCode, ExecutionCertainty, OperationStatus
from agent_workbench.contracts.execution_runtime import (
    EXECUTION_SUBPROTOCOL_VERSION,
    ExecutionControlSync,
)
from agent_workbench.contracts.tool import ExecutorCapabilities, ToolError, ToolResult
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
)
from agent_workbench.persistence.repositories import (
    ExecutionResourceRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import RunRow
from agent_workbench.tools.remote import json_text, prepare_operation
from agent_workbench.tools.runtime import Toolbox


class ExecutionUnavailable(RuntimeError):
    """执行能力不可用。**确定失败**：不产生副作用，重试同样会失败。"""

    def __init__(self, code: str, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def failure_result(
    operation_id: UUID | None, code: str, message: str, *, hint: str = ""
) -> ToolResult:
    return ToolResult(
        operation_id=operation_id or UUID(int=0),
        status=OperationStatus.FAILED,
        summary=message,
        error=ToolError(code=code, message=message, retryable=False, recovery_hint=hint or None),
        execution_certainty=ExecutionCertainty.NOT_APPLIED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


def uncertain_result(operation_id: UUID, message: str, *, details: str = "") -> ToolResult:
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.UNKNOWN,
        summary=message,
        error=ToolError(
            code=str(ErrorCode.EXECUTOR_UNAVAILABLE),
            message=details or message,
            retryable=True,
            recovery_hint=(
                "按同一 operation_id 重新提交只会得到原资源（幂等），不会产生第二个副作用；"
                "也可先用资源列表核对实际状态。"
            ),
        ),
        execution_certainty=ExecutionCertainty.UNCERTAIN,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


async def load_capabilities(toolbox: Toolbox) -> ExecutorCapabilities:
    return await toolbox.executor.capabilities()


def require_capability(caps: ExecutorCapabilities, name: str) -> None:
    """执行能力门：不可用时给出**确定原因**，不猜测、不回退本机执行。"""
    versions = caps.execution_runtime_versions or []
    if not caps.execution:
        raise ExecutionUnavailable(
            "dependency_unavailable",
            "执行岛未声明执行子协议（/execution/v1）；新执行工具在该端确定不可用，"
            "**不会**回退到控制端本机执行命令。",
            hint="请由部署流程升级执行端；升级前请使用既有 HTTP/浏览器/文件能力。",
        )
    if EXECUTION_SUBPROTOCOL_VERSION not in versions and versions:
        raise ExecutionUnavailable(
            "dependency_unavailable",
            f"执行岛支持的执行子协议版本为 {versions}，控制端需要 "
            f"{EXECUTION_SUBPROTOCOL_VERSION}；拒绝在版本不匹配时执行。",
        )
    if caps.execution_mode != "trusted":
        raise ExecutionUnavailable(
            "execution_disabled",
            "执行岛未启用可信执行模式（EXECUTOR_EXECUTION_MODE != trusted）；"
            "命令/脚本/终端能力当前关闭。",
            hint=caps.execution_reason(name)
            or "个人部署可显式设置 EXECUTOR_EXECUTION_MODE=trusted 后重试。",
        )
    if str(caps.execution.get(name, "unavailable")) != "available":
        raise ExecutionUnavailable(
            "dependency_unavailable",
            f"执行岛报告能力 {name} 不可用。",
            hint=caps.execution_reason(name),
        )


async def sync_fence(toolbox: Toolbox, *, revocations: list[dict[str, Any]] | None = None) -> int:
    """同步受信 fence 并返回执行端当前 seq。旧方向的同步不会被应用。"""
    from sqlalchemy import select

    from agent_workbench.contracts.execution_runtime import DelegationRevocation

    def _revocation_model(item: dict[str, Any]) -> DelegationRevocation:
        return DelegationRevocation.model_validate(item)

    from agent_workbench.domain.execution_delegations import revocation_payload

    async with toolbox.db.session() as db:
        persisted_revocations = await revocation_payload(db, toolbox.session_id)
        run = (
            await db.execute(select(RunRow).where(RunRow.id == toolbox.run_id))
        ).scalar_one_or_none()
    current_version = int(run.control_version) if run is not None else toolbox.control_version
    payload = ExecutionControlSync(
        session_id=str(toolbox.session_id),
        run_id=str(toolbox.run_id),
        owner=toolbox.owner,
        owner_generation=toolbox.owner_generation,
        control_version=current_version,
        expected_seq=toolbox.execution_fence_seq,
        revocations=[
            _revocation_model(item) for item in [*persisted_revocations, *(revocations or [])]
        ],
        note="",
    ).model_dump(mode="json")
    ack = await toolbox.executor.execution_sync_control(payload)
    if not ack.get("applied"):
        # 乐观并发失败：用执行端当前 seq 重试一次；仍冲突则如实失败。
        fence = ack.get("fence") or {}
        payload["expected_seq"] = fence.get("seq")
        ack = await toolbox.executor.execution_sync_control(payload)
        if not ack.get("applied"):
            raise ExecutionUnavailable(
                "stale_control",
                f"执行端拒绝本次控制同步：{ack.get('reason') or '未知原因'}",
            )
    fence = ack.get("fence") or {}
    seq = int(fence.get("seq") or 0)
    toolbox.execution_fence_seq = seq
    return seq


async def prepare_execution_operation(
    toolbox: Toolbox,
    *,
    tool_call_id: str,
    tool_name: str,
    action: str,
    arguments: dict[str, Any],
) -> tuple[UUID, dict[str, Any], bool]:
    """准备操作身份并构造操作包。返回 `(operation_id, envelope, replayed)`。"""
    from agent_workbench.contracts.enums import ToolEffectClass

    operation_id, replayed = await prepare_operation(
        toolbox,
        tool_call_id,
        tool_name,
        {"action": action, **arguments},
        effect_class=ToolEffectClass.SIDE_EFFECTING,
    )
    digest = None
    async with toolbox.db.session() as db:
        row = await ToolOperationRepository(db).get(operation_id)
        digest = row.request_digest if row is not None else None
    if digest is None:
        raise ExecutionUnavailable("internal_error", "操作身份持久化失败：找不到操作记录")
    from agent_workbench.contracts.execution_runtime import execution_request_digest

    digest = execution_request_digest(action, arguments)
    return operation_id, {"digest": digest, "action": action, "arguments": arguments}, replayed


def build_envelope(
    toolbox: Toolbox,
    *,
    operation_id: UUID,
    tool_call_id: str,
    action: str,
    arguments: dict[str, Any],
    request_digest: str,
    wait_ms: int = 0,
    delegation_id: str | None = None,
    delegation_revision: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_SUBPROTOCOL_VERSION,
        "operation_id": str(operation_id),
        "session_id": str(toolbox.session_id),
        "run_id": str(toolbox.run_id),
        "tool_call_id": tool_call_id,
        "request_digest": request_digest,
        "action": action,
        "arguments": arguments,
        "owner_generation": toolbox.owner_generation,
        "dispatch_control_version": toolbox.control_version,
        "session_control_seq": int(toolbox.execution_fence_seq or 0),
        "delegation_id": delegation_id or getattr(toolbox, "execution_delegation_id", None),
        "delegation_revision": (
            delegation_revision if delegation_revision is not None
            else getattr(toolbox, "execution_delegation_revision", None)
        ),
        "wait_ms": max(0, min(int(wait_ms), 60_000)),
        "created_at": datetime.now(UTC).isoformat(),
    }


async def dispatch_operation(
    toolbox: Toolbox,
    operation_id: UUID,
    envelope: dict[str, Any],
) -> tuple[dict[str, Any] | None, ToolResult | None]:
    """提交操作包。返回 `(receipt, failure_result)`；两者恰有一个非空。"""
    try:
        receipt = await toolbox.executor.execution_submit(envelope)
    except ExecutorRejected as exc:
        await _record_outcome(
            toolbox,
            operation_id=operation_id,
            status=str(OperationStatus.FAILED),
            failure=f"被执行岛拒绝：{exc}",
            job_id=None,
            resource_id=None,
        )
        code = _extract_code(str(exc)) or str(ErrorCode.VALIDATION_FAILED)
        return None, failure_result(
            operation_id,
            code,
            f"执行岛明确拒绝了该操作，请求未执行：{exc}",
            hint="参数/能力/控制状态不匹配；请按错误码修正后重新发起新的逻辑操作。",
        )
    except ExecutorProtocolError as exc:
        await _record_outcome(
            toolbox,
            operation_id=operation_id,
            status=str(OperationStatus.UNKNOWN),
            failure=f"协议错误：{exc}",
            job_id=None,
            resource_id=None,
        )
        return None, uncertain_result(operation_id, "执行岛返回了不符合协议的数据", details=str(exc))
    except ExecutorConnectionError as exc:
        return None, uncertain_result(
            operation_id,
            "执行岛不可达，无法确认操作是否已受理；不要直接重复提交",
            details=str(exc),
        )
    return receipt, None


def _extract_code(text: str) -> str | None:
    for code in (
        "execution_disabled",
        "dependency_unavailable",
        "operation_conflict",
        "stale_control",
        "resource_not_found",
        "resource_owner_mismatch",
        "interaction_lost",
        "input_sequence_conflict",
        "resource_limit",
        "output_gap",
        "invalid_encoding",
        "validation_failed",
    ):
        if code in text:
            return code
    return None


async def _record_outcome(
    toolbox: Toolbox,
    *,
    operation_id: UUID,
    status: str,
    job_id: UUID | None,
    resource_id: str | None,
    failure: str | None = None,
    receipt: dict[str, Any] | None = None,
) -> None:
    async with toolbox.db.transaction() as db:
        operations = ToolOperationRepository(db)
        if job_id is not None:
            await operations.attach_job(operation_id, job_id, status=str(status))
        await operations.mark_result(operation_id, status=str(status))
        if job_id is not None:
            await ToolJobRepository(db).upsert(
                job_id=job_id,
                operation_id=operation_id,
                executor_id=toolbox.settings.executor_id,
                state=str(status),
                sync_status="in_sync",
            )
    if failure:
        from agent_workbench.contracts.events import EventType
        from agent_workbench.observability.events import append_event

        async with toolbox.db.transaction() as db:
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.ERROR,
                payload={
                    "scope": "execution_submit",
                    "operation_id": str(operation_id),
                    "message": failure,
                },
            )
    if resource_id:
        await project_resource(toolbox, resource_id, receipt or {})


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def project_resource(
    toolbox: Toolbox, resource_id: str, receipt: dict[str, Any]
) -> None:
    """把观察到的资源状态写入控制端只读投影（真实状态仍归执行端）。"""
    parsed = _uuid_or_none(resource_id)
    if parsed is None:
        return
    resource = receipt.get("resource") or {}
    views = resource if isinstance(resource, dict) else {}
    kind = str(receipt.get("action") or "").removeprefix("run_")
    kind = {"command": "command", "script": "script", "monitor": "monitor", "start_monitor": "monitor"}.get(kind, kind)
    if receipt.get("terminal_id"):
        kind = "terminal"
    state = str(views.get("state") or receipt.get("process_state") or "running")
    async with toolbox.db.transaction() as db:
        await ExecutionResourceRepository(db).upsert(
            resource_id=parsed,
            session_id=toolbox.session_id,
            executor_id=toolbox.settings.executor_id,
            kind=kind or "command",
            state=state,
            interaction_state=str(
                views.get("interaction_state") or receipt.get("interaction_state") or "not_applicable"
            ),
            mode=str(views.get("mode") or ""),
            purpose=str(views.get("purpose") or ""),
            origin_run_id=toolbox.run_id,
            origin_operation_id=_uuid_or_none(receipt.get("operation_id")),
            exit_code=views.get("exit_code", receipt.get("exit_code")),
            termination_reason=views.get("termination_reason") or receipt.get("termination_reason"),
            output_limit_action=str(views.get("output_limit_action") or "preserve_process"),
            quota={
                "output_bytes": views.get("output_bytes"),
                "dropped_bytes": views.get("dropped_bytes"),
                "output_limit_bytes": views.get("output_limit_bytes"),
            },
            detail={"notes": views.get("notes") or [], **(
                {"completion_role": receipt["completion_role"]} if "completion_role" in receipt else {}
            )},
        )


def receipt_text(receipt: dict[str, Any]) -> str:
    return json.dumps(receipt, ensure_ascii=False)


def receipt_summary(receipt: dict[str, Any]) -> str:
    state = receipt.get("process_state") or "running"
    reason = receipt.get("wait_reason")
    parts = [f"资源 {receipt.get('resource_id')}", f"状态 {state}"]
    if reason:
        parts.append(f"等待结果 {reason}")
    if receipt.get("deduplicated"):
        parts.append("幂等重放：返回原资源")
    return "；".join(parts)


__all__ = [
    "ExecutionUnavailable",
    "build_envelope",
    "dispatch_operation",
    "failure_result",
    "json_text",
    "load_capabilities",
    "prepare_execution_operation",
    "project_resource",
    "receipt_summary",
    "receipt_text",
    "require_capability",
    "sync_fence",
    "uncertain_result",
]
