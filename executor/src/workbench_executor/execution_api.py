"""执行端 `/execution/v1` 子协议路由（挂入既有 FastAPI app）。

最小路由集合（规格 §13）：
- POST `/execution/v1/control`                同步会话 fence
- POST `/execution/v1/operations`             接收命令/脚本/终端副作用
- GET  `/execution/v1/operations/{id}`        按操作身份核对
- GET  `/execution/v1/resources`              session 分页列表
- GET  `/execution/v1/resources/{id}`         状态与实际能力
- GET  `/execution/v1/resources/{id}/output`  有界流或 screen
- GET  `/execution/v1/events`                 持久补读
- GET  `/execution/v1/environment`            环境发现

写操作集中经 operations；旧 `/jobs` 等接口保留并继续服务 HTTP/浏览器/文件能力。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from workbench_executor._generated.contracts import execution_runtime as proto
from workbench_executor.command_runtime import RuntimeRejected

#: 大额等待上限：操作受理后在执行端内等待的上限（控制面通常远小于此值）。
MAX_SERVER_WAIT_MS = 60_000
SETTLE_POLL_SECONDS = 0.15


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def create_router(get_state: Callable[[], Any], require_token: Callable[..., Any]) -> APIRouter:
    router = APIRouter(prefix=proto.EXECUTION_PREFIX, tags=["execution"])

    # ------------------------------------------------------------------ #
    # 控制 fence
    # ------------------------------------------------------------------ #

    @router.post("/control", dependencies=[Depends(require_token)])
    async def sync_control(payload: proto.ExecutionControlSync) -> dict[str, Any]:
        state = get_state()
        applied, view, reason = await state.ledger.sync_fence(
            session_id=payload.session_id,
            run_id=payload.run_id,
            owner=payload.owner,
            owner_generation=payload.owner_generation,
            control_version=payload.control_version,
            revocations=[item.model_dump() for item in payload.revocations],
            expected_seq=payload.expected_seq,
        )
        if applied:
            await state.ledger.append_event(
                event_id=str(uuid.uuid4()),
                type="control.synced",
                session_id=payload.session_id,
                payload={
                    "run_id": payload.run_id,
                    "owner_generation": payload.owner_generation,
                    "control_version": payload.control_version,
                },
            )
        ack = proto.ExecutionControlAck(
            session_id=payload.session_id,
            applied=applied,
            fence=proto.FenceView(**view),
            reason=reason,
        )
        return ack.model_dump(mode="json")

    async def _require_current_fence(
        state: Any, envelope: proto.ExecutionOperationEnvelope
    ) -> dict[str, Any]:
        fence = await state.ledger.get_fence(envelope.session_id)
        if fence is None:
            raise RuntimeRejected(
                "stale_control",
                "执行端尚未收到该会话的控制同步；拒绝在无 fence 的情况下执行副作用操作",
            )
        if fence["run_id"] != envelope.run_id:
            raise RuntimeRejected(
                "stale_control",
                "操作所属 run 与执行端当前 fence 不一致（旧 run 或方向已变）",
                details={"fence_run_id": fence["run_id"], "operation_run_id": envelope.run_id},
            )
        if int(fence["owner_generation"]) != int(envelope.owner_generation):
            raise RuntimeRejected(
                "stale_control",
                "操作所属 owner generation 已过期",
                details={
                    "fence_generation": fence["owner_generation"],
                    "operation_generation": envelope.owner_generation,
                },
            )
        if int(fence["control_version"]) != int(envelope.dispatch_control_version):
            raise RuntimeRejected(
                "stale_control",
                "操作固定的是旧控制版本；用户方向可能已变化，拒绝执行",
                details={
                    "fence_control_version": fence["control_version"],
                    "operation_control_version": envelope.dispatch_control_version,
                },
            )
        if int(envelope.session_control_seq) > int(fence["seq"]):
            raise RuntimeRejected(
                "stale_control",
                "操作声明了执行端尚未见过的 fence 序号；拒绝自填提权",
            )
        if envelope.delegation_id:
            revocations = {
                str(item.get("delegation_id")): int(item.get("revision", 0))
                for item in fence.get("revocations") or []
                if isinstance(item, dict)
            }
            revoked_revision = revocations.get(envelope.delegation_id)
            if revoked_revision is not None and int(envelope.delegation_revision or 0) <= revoked_revision:
                raise RuntimeRejected(
                    "stale_control",
                    "该委托已被撤销；旧 revision 的操作不再获得执行权",
                )
        return fence

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #

    @router.post("/operations", dependencies=[Depends(require_token)])
    async def submit_operation(payload: dict[str, Any]) -> JSONResponse:
        state = get_state()
        try:
            envelope = proto.ExecutionOperationEnvelope.model_validate(payload)
        except Exception as exc:
            raise _http_error("validation_failed", f"操作包结构非法：{exc}", 422) from exc
        try:
            parsed = proto.validate_arguments(envelope.action, envelope.arguments)
        except Exception as exc:
            raise _http_error("validation_failed", f"参数非法：{exc}", 422) from exc

        if not state.execution_available:
            raise _http_error(
                "execution_disabled",
                state.execution_unavailable_reason or "执行底座未启用（EXECUTOR_EXECUTION_MODE）",
                503,
            )

        if envelope.request_digest != proto.execution_request_digest(envelope.action, envelope.arguments):
            raise RuntimeRejected("validation_failed", "请求摘要与实际操作载荷不一致")
        if envelope.arguments.get("workspace_id") not in {None, "", envelope.session_id}:
            raise RuntimeRejected("resource_owner_mismatch", "执行工作区不属于该会话")
        existing = await state.ledger.get_operation(envelope.operation_id)
        if existing is not None and existing.session_id != envelope.session_id:
            raise RuntimeRejected("resource_owner_mismatch", "操作不属于该会话")
        resource_id = envelope.arguments.get("terminal_id") or envelope.arguments.get("resource_id")
        if resource_id:
            resource_session = await _resource_session(state, str(resource_id))
            if resource_session != envelope.session_id:
                raise RuntimeRejected("resource_owner_mismatch", "资源不属于该会话")

        await _require_current_fence(state, envelope)

        if envelope.action in {"run_command", "run_script", "start_monitor", "terminal_open"}:
            async with state.execution_admission_lock:
                await _require_current_fence(state, envelope)
                receipt = await _dispatch(state, envelope, parsed)
        else:
            receipt = await _dispatch(state, envelope, parsed)
        if envelope.wait_ms and receipt.resource_id and _is_async_receipt(receipt):
            receipt = await _settle(
                state, receipt, resource_id=receipt.resource_id, wait_ms=envelope.wait_ms
            )
        return JSONResponse(content=receipt.model_dump(mode="json"))

    async def _dispatch(
        state: Any,
        envelope: proto.ExecutionOperationEnvelope,
        parsed: Any,
    ) -> proto.OperationReceipt:
        action = envelope.action
        if action in {"run_command", "run_script", "start_monitor"}:
            existing = await state.ledger.get_operation(envelope.operation_id)
            if existing is not None and existing.request_digest == envelope.request_digest:
                return state.command_runtime._receipt_from_record(existing, deduplicated=True)
            kind = {"run_command": "command", "run_script": "script", "start_monitor": "monitor"}[
                action
            ]
            return await state.command_runtime.accept(
                envelope,
                arguments=envelope.arguments,
                kind=kind,
                purpose=parsed.description,
            )
        if action == "terminal_open":
            receipt, _reason = await state.terminals.open(envelope, envelope.arguments)
            return receipt
        if action == "terminal_send":
            return await state.terminals.send(envelope, parsed)
        if action == "terminal_resize":
            return await state.terminals.resize(envelope, parsed)
        if action == "terminal_close":
            return await state.terminals.close(envelope, parsed)
        if action == "cancel_resource":
            record = await state.ledger.get_terminal(parsed.resource_id)
            if record is not None:
                close_args = proto.TerminalCloseArguments(terminal_id=parsed.resource_id)
                return await state.terminals.close(
                    envelope, close_args, expected_session=envelope.session_id
                )
            result = await state.command_runtime.cancel(
                parsed.resource_id, request_id=envelope.operation_id
            )
            state_value = str(result["state"])
            return proto.OperationReceipt(
                operation_id=envelope.operation_id,
                action="cancel_resource",
                status="succeeded" if result["process_confirmed_exited"] else "unknown",
                resource_id=parsed.resource_id,
                process_confirmed_exited=bool(result["process_confirmed_exited"]),
                process_state=(
                    proto.ResourceStatus.CLOSED if result["process_confirmed_exited"] else proto.ResourceStatus(state_value)
                    if state_value in {item.value for item in proto.ResourceStatus}
                    else proto.ResourceStatus.UNKNOWN
                ),
                termination_reason=(
                    proto.TerminationReason.USER_CANCELLED
                    if result["process_confirmed_exited"]
                    else proto.TerminationReason.UNKNOWN
                ),
                accepted_at=_utcnow(),
                note=result["detail"],
            )
        raise RuntimeRejected("validation_failed", f"未知动作：{action}")

    def _is_async_receipt(receipt: proto.OperationReceipt) -> bool:
        return receipt.process_state in {
            proto.ResourceStatus.STARTING,
            proto.ResourceStatus.RUNNING,
        }

    async def _settle(
        state: Any, receipt: proto.OperationReceipt, *, resource_id: str, wait_ms: int
    ) -> proto.OperationReceipt:
        """受理后在执行端内等待至多 wait_ms；到期**只返回原 job**，不重新启动。"""
        bounded = min(int(wait_ms), MAX_SERVER_WAIT_MS)
        deadline = asyncio.get_running_loop().time() + bounded / 1000.0
        while True:
            status = await state.command_runtime.status_for(resource_id)
            if status is None:
                status = await state.terminals.status(resource_id)
            if status is not None and status.state in {
                proto.ResourceStatus.EXITED,
                proto.ResourceStatus.CLOSED,
                proto.ResourceStatus.UNKNOWN,
            }:
                receipt.process_state = status.state
                receipt.exit_code = status.exit_code
                receipt.termination_reason = status.termination_reason
                receipt.resource = status
                receipt.wait_reason = proto.WaitReason.EXITED
                receipt.note = "任务已到达确定状态；退出码与原因来自执行台账。"
                try:
                    receipt.output = await _first_page(state, resource_id)
                except Exception:
                    receipt.output = None
                return receipt
            if asyncio.get_running_loop().time() > deadline:
                receipt.wait_reason = proto.WaitReason.WAIT_TIMEOUT
                receipt.note = (
                    "等待超时：返回同一个 job。任务仍在运行，没有被取消，也没有被重新提交。"
                )
                if status is not None:
                    receipt.resource = status
                    receipt.process_state = status.state
                return receipt
            await asyncio.sleep(SETTLE_POLL_SECONDS)

    async def _first_page(state: Any, resource_id: str) -> proto.OutputPageV2 | None:
        record = await state.ledger.get_terminal(resource_id)
        if record is not None:
            return await state.terminals.read_page(
                resource_id, cursor=None, stream="stdout", limit=16384, view="stream"
            )
        return await state.command_runtime.read_output(
            resource_id, stream="stdout", cursor=None, limit=16384
        )

    @router.get("/operations/{operation_id}", dependencies=[Depends(require_token)])
    async def get_operation(operation_id: str) -> dict[str, Any]:
        state = get_state()
        record = await state.ledger.get_operation(operation_id)
        if record is None:
            raise _http_error("not_found", f"操作不存在：{operation_id}", 404)
        return {
            "operation_id": record.operation_id,
            "session_id": record.session_id,
            "run_id": record.run_id,
            "action": record.action,
            "resource_id": record.resource_id,
            "status": record.status,
            "accepted_at": record.accepted_at,
            "executed_at": record.executed_at,
            "receipt": record.receipt,
        }

    # ------------------------------------------------------------------ #
    # 资源
    # ------------------------------------------------------------------ #

    @router.get("/resources", dependencies=[Depends(require_token)])
    async def list_resources(
        session_id: str = Query(...),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        kind: str | None = Query(default=None),
    ) -> dict[str, Any]:
        state = get_state()
        # 台账先合并分页，终端不能再独立 offset 后重复拼接，否则会漏资源。
        rows, total = await state.ledger.list_session_resources(session_id, limit=limit, offset=offset)
        resources = [state.command_runtime._status_from_job(item["record"])
                     if item["kind"] == "job" else state.terminals._status_from_row(item["record"])
                     for item in rows]
        if kind is not None:
            resources = [item for item in resources if item.kind.value == kind]
        return {"executor_id": state.settings.executor_id, "session_id": session_id,
                   "resources": [item.model_dump(mode="json") for item in resources],
                   "total": total, "next_cursor": str(offset+limit) if offset+limit < total else None,
                   "session_quota": {"log_budget_used_bytes": await state.ledger.session_budget_used(session_id)}}

    @router.get("/resources/{resource_id}", dependencies=[Depends(require_token)])
    async def get_resource(resource_id: str) -> dict[str, Any]:
        state = get_state()
        status = await state.command_runtime.status_for(resource_id)
        if status is None:
            status = await state.terminals.status(resource_id)
        if status is None:
            raise _http_error("resource_not_found", f"资源不存在：{resource_id}", 404)
        return status.model_dump(mode="json")

    @router.get("/resources/{resource_id}/output", dependencies=[Depends(require_token)])
    async def read_resource_output(
        resource_id: str,
        stream: str = Query(default="stdout"),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=16384, ge=1, le=proto.MAX_OUTPUT_PAGE_BYTES),
        view: str = Query(default="stream"),
        max_lines: int | None = Query(default=None, ge=1, le=5000),
        session_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        state = get_state()
        if session_id:
            # 归属核对：跨 session 一律拒绝（跨 run 续用允许）。
            owner = await _resource_session(state, resource_id)
            if owner is not None and owner and owner != session_id:
                raise _http_error("resource_owner_mismatch", "资源不属于该会话", 403)
        terminal = await state.ledger.get_terminal(resource_id)
        if terminal is not None:
            if view == "screen":
                page = await state.terminals.read_page(
                    resource_id, cursor=None, stream="stdout", limit=limit, view="screen"
                )
                return {**page.model_dump(mode="json"), "view": "screen"}
            page = await state.terminals.read_page(
                resource_id, cursor=cursor, stream=stream, limit=limit, view="stream", max_lines=max_lines
            )
            return page.model_dump(mode="json")
        page = await state.command_runtime.read_output(
            resource_id, stream=stream, cursor=cursor, limit=limit
        )
        return page.model_dump(mode="json")

    @router.get("/resources/{resource_id}/raw-output", dependencies=[Depends(require_token)])
    async def raw_output(
        resource_id: str,
        session_id: str,
        stream: str = "stdout",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=262144, ge=1, le=1048576),
    ) -> dict[str, Any]:
        state = get_state()
        if await _resource_session(state, resource_id) != session_id:
            raise _http_error("resource_owner_mismatch", "资源不属于该会话", 403)
        if stream not in {"stdout", "stderr"}:
            raise _http_error("validation_failed", "stream 必须为 stdout 或 stderr", 422)
        terminal = await state.ledger.get_terminal(resource_id)
        if terminal is not None:
            if stream != "stdout":
                raise _http_error("validation_failed", "PTY 只提供合流 stdout", 422)
            root = Path(terminal["log_dir"])
        else:
            root = state.command_runtime.job_dir(resource_id)
        path = root / (stream + ".log")
        if not path.is_file():
            raise _http_error("resource_not_found", "该资源尚无原始输出文件", 404)
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(offset)
            data = handle.read(min(limit, max(0, size - offset)))
        return proto.RawOutputSnapshot(
            resource_id=resource_id, stream=stream, stored_offset=offset,
            next_offset=offset + len(data), observed_size=size,
            sha256=hashlib.sha256(data).hexdigest(), data_base64=base64.b64encode(data).decode(),
            captured_at=_utcnow(), at_current_end=offset + len(data) >= size,
            note="快照只覆盖本次已存字节区间；采集丢弃区间须结合 output-index/输出页核对。",
        ).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # 事件与环境
    # ------------------------------------------------------------------ #

    @router.get("/events", dependencies=[Depends(require_token)])
    async def read_events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        state = get_state()
        rows = await state.ledger.read_events(after=after, limit=limit)
        events = [
            proto.ExecutionEventRecord(
                seq=int(row["seq"]),
                event_id=str(row["event_id"]),
                executor_id=state.settings.executor_id,
                session_id=str(row["session_id"]),
                resource_id=row["resource_id"],
                operation_id=row["operation_id"],
                type=str(row["type"]),
                occurred_at=str(row["occurred_at"]),
                payload=json.loads(row["payload_json"] or "{}"),
                availability=proto.OutputAvailability(str(row["availability"])),
            )
            for row in rows
        ]
        next_after = events[-1].seq if events else after
        has_more = False
        if len(rows) == limit:
            probe = await state.ledger.read_events(after=next_after, limit=1)
            has_more = bool(probe)
        return proto.ExecutionEventPage(
            executor_id=state.settings.executor_id,
            events=events,
            next_after=next_after,
            has_more=has_more,
        ).model_dump(mode="json")

    @router.get("/environment", dependencies=[Depends(require_token)])
    async def get_environment() -> dict[str, Any]:
        state = get_state()
        from workbench_executor.execution_environment import discover_environment, workspace_bytes

        report = discover_environment(
            state.settings,
            workspace_bytes_used=workspace_bytes(state.settings.workspace_root),
        )
        return proto.ExecutionEnvironmentReport.model_validate(report).model_dump(mode="json")

    return router


def _http_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def _resource_session(state: Any, resource_id: str) -> str | None:
    """资源的会话归属（终端行或执行任务行）。查不到返回 None（由调用方决定）。"""
    row = await state.ledger.get_terminal(resource_id)
    if row is not None:
        return str(row["session_id"])
    job = await state.ledger.get(resource_id)
    if job is not None:
        return str(job.session_id)
    return None
