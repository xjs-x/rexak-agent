"""后台任务工具：`get_job`、`read_job_output`、`cancel_job`。

规则（§8.3）：
- `output cursor` 增量读取；**不鼓励模型密集轮询**；
- 给模型显示活动 job 简表与状态，不把所有输出反复注入；
- 取消要区分"已确认进程退出"与"状态未知"——后者不能显示为"已停止"；
- **按 session 跨 run 解析**（同会话续用允许，跨会话不允许）；保留原派发方向标识。

取消使用**持久操作 ID**产生取消请求身份：模型不再需要自己编造 UUID
（旧的 `request_id` 参数保留兼容，但限制归属）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4, uuid5

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolCallId, tool
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import ErrorCode, OperationStatus
from agent_workbench.contracts.tool import ToolError, ToolResult
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
)
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    ExecutionResourceRepository,
    ToolJobRepository,
)
from agent_workbench.persistence.tables import (
    ArtifactProvenanceRow,
    ExecutionResourceRow,
    RunRow,
    ToolOperationRow,
)
from agent_workbench.tools.execution_dispatch import (
    build_envelope,
    dispatch_operation,
    load_capabilities,
    prepare_execution_operation,
    project_resource,
)
from agent_workbench.tools.runtime import Toolbox

DEFAULT_OUTPUT_LIMIT = 16384


def _result(payload: dict[str, object], summary: str, job_id: UUID | None) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        job_id=job_id,
        status=OperationStatus.SUCCEEDED,
        summary=summary,
        data=dict(payload),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ).model_dump_json()


def _error_result(
    summary: str, code: ErrorCode | str, message: str, job_id: UUID | None,
    *, recovery_hint: str | None = None,
) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        job_id=job_id,
        status=OperationStatus.FAILED,
        summary=summary,
        error=ToolError(code=str(code), message=message, recovery_hint=recovery_hint),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ).model_dump_json()


async def _resolve_job_id(toolbox: Toolbox, job_id: str) -> UUID:
    """解析 job id（**按 session**，支持前缀简写）。跨会话一律拒绝。"""
    candidate = job_id.strip()
    if not candidate:
        raise ValueError("job_id 不得为空")
    try:
        parsed: UUID | None = UUID(candidate)
    except ValueError:
        parsed = None
    async with toolbox.db.session() as db:
        session_id = getattr(toolbox, "session_id", None)
        if session_id is None:
            run = await db.get(RunRow, toolbox.run_id)
            if run is None:
                raise ValueError("当前运行不存在")
            session_id = run.session_id
            toolbox.session_id = session_id
        projections = list(
            (
                await db.execute(
                    select(ExecutionResourceRow).where(
                        ExecutionResourceRow.session_id == session_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # 旧接口（HTTP/浏览器/文件）的 job 也在同会话内可管理
        legacy = list(
            (
                await db.execute(
                    select(ToolOperationRow.job_id).where(
                        ToolOperationRow.session_id == session_id,
                        ToolOperationRow.job_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    known: dict[str, str] = {str(row.resource_id): str(row.kind) for row in projections}
    for job in legacy:
        if job is not None:
            known.setdefault(str(job), "legacy")
    if parsed is not None:
        if str(parsed) in known:
            return parsed
        raise ValueError(f"当前会话中找不到该 job/资源：{candidate}")
    matches = [rid for rid in known if rid.startswith(candidate)]
    if len(matches) == 1:
        return UUID(matches[0])
    if not matches:
        raise ValueError(
            f"找不到 job：{candidate}。可从 list_jobs 或工具返回中获取完整 ID。"
        )
    raise ValueError(f"前缀匹配到 {len(matches)} 条，请给出更完整的 ID")


async def _job_kind(toolbox: Toolbox, job_id: UUID) -> str:
    async with toolbox.db.session() as db:
        row = await ExecutionResourceRepository(db).get(job_id)
    return str(row.kind) if row is not None else "legacy"


async def _job_direction(toolbox: Toolbox, job_id: UUID) -> dict[str, object]:
    """读取 job 的派发版本；旧数据保留未知状态，不猜测为当前方向。"""
    async with toolbox.db.session() as db:
        row = (
            await db.execute(
                select(ToolOperationRow.dispatch_control_version, RunRow.control_version)
                .join(RunRow, RunRow.id == ToolOperationRow.run_id)
                .where(
                    ToolOperationRow.session_id == toolbox.session_id,
                    ToolOperationRow.job_id == job_id,
                )
            )
        ).one_or_none()
    if row is None:
        return {
            "dispatch_control_version": None,
            "current_control_version": None,
            "direction_status": "unrecorded",
        }
    dispatched, current = row
    return {
        "dispatch_control_version": dispatched,
        "current_control_version": current,
        "direction_status": (
            "unknown"
            if dispatched is None
            else "current"
            if dispatched == current
            else "historical"
        ),
    }


def _direction_warning(direction: dict[str, object]) -> str:
    status = direction["direction_status"]
    if status == "historical":
        return "；该 job 属于旧方向，输出只是历史记录，采用前须按当前目标重新核对"
    if status == "unknown":
        return "；该历史 job 缺少派发版本，采用输出前须按当前目标重新核对"
    return ""


async def _execution_view(toolbox: Toolbox, job_id: UUID) -> dict[str, Any] | None:
    """执行底座资源：优先返回执行端真实状态，失败时退回本地投影。"""
    async with toolbox.db.session() as db:
        projection = await ExecutionResourceRepository(db).get(job_id)
    if projection is None:
        return None
    caps = await load_capabilities(toolbox)
    if not caps.execution:
        return None
    try:
        return await toolbox.executor.execution_resource(str(job_id))
    except (ExecutorConnectionError, ExecutorProtocolError):
        async with toolbox.db.session() as db:
            row = await ExecutionResourceRepository(db).get(job_id)
        if row is None:
            return None
        return {
            "resource_id": str(row.resource_id),
            "kind": row.kind,
            "state": row.state,
            "interaction_state": row.interaction_state,
            "exit_code": row.exit_code,
            "termination_reason": row.termination_reason,
            "sync_status": row.sync_status,
            "note": "执行岛不可达：以上为上次同步的投影，不是实时状态。",
        }


@tool("get_job")
async def get_job(runtime: ToolRuntime[RunContext], job_id: str) -> str:
    """查询一个后台任务/执行资源的状态（**不做密集轮询**；有事件会通知你）。

    Args:
        job_id: 任务/资源 ID；可用前 8 位简写（按当前会话解析）。

    Returns:
        JobSnapshot 或 ResourceStatusView 的 JSON。`state=unknown` 表示无法确认终态，
        需要人工核对，不要自动重做该操作。
    """
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_job_id(toolbox, job_id)
    except ValueError as exc:
        return _error_result("无法解析 job_id", ErrorCode.VALIDATION_FAILED, str(exc), None)

    kind = await _job_kind(toolbox, resolved)
    direction = await _job_direction(toolbox, resolved)

    view = await _execution_view(toolbox, resolved)
    if view is not None:
        payload = {**view, **direction, "kind": view.get("kind") or kind}
        return _result(
            payload,
            f"{payload.get('kind')} {resolved} 当前状态：{payload.get('state')}"
            f"{_direction_warning(direction)}",
            resolved,
        )

    try:
        snapshot = await toolbox.executor.get_job(resolved)
    except ExecutorConnectionError as exc:
        return _error_result(
            "执行岛不可达，无法查询任务状态；这不是任务失败",
            ErrorCode.EXECUTOR_UNAVAILABLE,
            str(exc),
            resolved,
        )
    except ExecutorProtocolError as exc:
        return _error_result(
            "执行岛返回了不符合协议的数据", ErrorCode.EXECUTOR_INCOMPATIBLE, str(exc), resolved
        )

    async with toolbox.db.transaction() as db:
        await ToolJobRepository(db).upsert(
            job_id=resolved,
            operation_id=UUID(int=0) if snapshot.operation_id is None else snapshot.operation_id,
            executor_id=toolbox.settings.executor_id,
            state=str(snapshot.state),
            output_cursor=snapshot.output_cursor,
            executor_started_at=snapshot.started_at,
            executor_finished_at=snapshot.finished_at,
        )
    payload = {**snapshot.model_dump(mode="json"), **direction}
    return _result(
        payload,
        f"job {resolved} 当前状态：{snapshot.state}{_direction_warning(direction)}",
        resolved,
    )


@tool("read_job_output")
async def read_job_output(
    runtime: ToolRuntime[RunContext],
    job_id: str,
    cursor: str | None = None,
    stream: str = "stdout",
    limit: int = DEFAULT_OUTPUT_LIMIT,
) -> str:
    """**增量**读取后台任务/资源的输出。

    Args:
        job_id: 任务/资源 ID；可用前 8 位简写。
        cursor: 上次返回的 next_cursor；不传表示从头读。
        stream: stdout 或 stderr（PTY 为合流，只能 stdout）。
        limit: 本次最多读取的字节数（默认 16 KiB）。

    Returns:
        OutputPage 的 JSON：text 是本段输出，next_cursor 用于下次继续读；
        `at_current_end` 只表示已读到当前末尾，**不代表程序已结束**；
        采集超限时 `missing_ranges` 明确标注缺失区间。
    """
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_job_id(toolbox, job_id)
    except ValueError as exc:
        return _error_result("无法解析 job_id", ErrorCode.VALIDATION_FAILED, str(exc), None)

    bounded = max(1, min(int(limit), 262144))
    caps = await load_capabilities(toolbox)
    async with toolbox.db.session() as db:
        projection = await ExecutionResourceRepository(db).get(resolved)
    if projection is not None and caps.execution and caps.execution_capable("command"):
        try:
            page = await toolbox.executor.execution_output(
                str(resolved),
                stream=stream,
                cursor=cursor,
                limit=bounded,
                session_id=str(toolbox.session_id),
            )
            direction = await _job_direction(toolbox, resolved)
            return _result(
                {**page, **direction},
                f"读取到 {page.get('byte_count', 0)} 字节输出{_direction_warning(direction)}",
                resolved,
            )
        except ExecutorRejected as exc:
            return _output_rejection(exc, resolved)
        except ExecutorConnectionError as exc:
            return _error_result(
                "执行岛不可达，无法读取输出", ErrorCode.EXECUTOR_UNAVAILABLE, str(exc), resolved
            )
        except ExecutorProtocolError as exc:
            return _error_result(
                "执行岛返回了不符合协议的数据", ErrorCode.EXECUTOR_INCOMPATIBLE, str(exc), resolved
            )

    try:
        page = await toolbox.executor.read_output(resolved, cursor, stream=stream, limit=bounded)
    except ExecutorRejected as exc:
        return _output_rejection(exc, resolved)
    except ExecutorConnectionError as exc:
        return _error_result(
            "执行岛不可达，无法读取输出", ErrorCode.EXECUTOR_UNAVAILABLE, str(exc), resolved
        )
    except ExecutorProtocolError as exc:
        return _error_result(
            "执行岛返回了不符合协议的数据", ErrorCode.EXECUTOR_INCOMPATIBLE, str(exc), resolved
        )

    direction = await _job_direction(toolbox, resolved)
    return _result(
        {**page.model_dump(mode="json"), **direction},
        f"读取到 {page.byte_count} 字节输出{_direction_warning(direction)}",
        resolved,
    )


def _output_rejection(exc: ExecutorRejected, job_id: UUID) -> str:
    return _error_result(
        "输出读取被执行岛拒绝，未读取其他资源的数据",
        exc.code or ErrorCode.VALIDATION_FAILED,
        str(exc),
        job_id,
        recovery_hint=(
            "cursor 只能用于产生它的同一资源；请使用该 job 上次返回的 next_cursor，"
            "或省略 cursor 从该资源开头读取。无需重新运行原命令。"
            if exc.code == "invalid_cursor"
            else "请按执行岛返回的错误码修正读取参数；不要重新运行原命令。"
        ),
    )


@tool("cancel_job")
async def cancel_job(
    runtime: ToolRuntime[RunContext],
    job_id: str,
    request_id: str | None = None,
) -> str:
    """取消一个后台任务/关闭一个终端（幂等；使用持久操作身份）。

    Args:
        job_id: 任务/资源 ID；可用前 8 位简写。
        request_id: **可省略**。省略时由系统用持久操作 ID 生成取消身份；
            显式提供时必须是当前会话中该资源的既有操作 ID（不能编造 UUID）。

    Returns:
        CancelReceipt/OperationReceipt 的 JSON。**重点看 `process_confirmed_exited`**：
        为 false 时必须向用户显示"状态未知/取消未确认"，不能显示"已停止"。
    """
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_job_id(toolbox, job_id)
    except ValueError as exc:
        return _error_result("参数非法", ErrorCode.VALIDATION_FAILED, str(exc), None)

    kind = await _job_kind(toolbox, resolved)
    tool_call_id = runtime.tool_call_id or ""
    explicit_request: UUID | None = None
    if request_id:
        try:
            explicit_request = UUID(request_id)
        except ValueError:
            return _error_result(
                "参数非法",
                ErrorCode.VALIDATION_FAILED,
                "request_id 必须是 UUID；或省略该参数让系统生成",
                resolved,
            )
        if not await _request_id_belongs_to_resource(toolbox, resolved, explicit_request):
            return _error_result(
                "request_id 归属不符",
                ErrorCode.VALIDATION_FAILED,
                "该 request_id 不是当前会话中这个资源的既有操作 ID；请省略该参数",
                resolved,
            )

    if kind in {"command", "script", "monitor", "terminal", "resource"}:
        return await _cancel_execution_resource(
            toolbox, resolved, kind=kind, tool_call_id=tool_call_id
        )

    # 旧接口兼容：HTTP/浏览器/文件任务的取消路径保持不变。
    try:
        request = explicit_request or UUID(int=0)
        if request == UUID(int=0):
            return _error_result(
                "参数非法",
                ErrorCode.VALIDATION_FAILED,
                "旧任务取消需要显式 request_id（幂等键）",
                resolved,
            )
        receipt = await toolbox.executor.cancel(resolved, request)
    except ExecutorConnectionError as exc:
        return _error_result(
            "执行岛不可达，取消请求未送达；任务可能仍在远端运行",
            ErrorCode.EXECUTOR_UNAVAILABLE,
            str(exc),
            resolved,
        )
    except ExecutorProtocolError as exc:
        return _error_result(
            "执行岛返回了不符合协议的数据", ErrorCode.EXECUTOR_INCOMPATIBLE, str(exc), resolved
        )

    async with toolbox.db.transaction() as db:
        await ToolJobRepository(db).upsert(
            job_id=resolved,
            operation_id=UUID(int=0),
            executor_id=toolbox.settings.executor_id,
            state=str(receipt.state),
            cancel_requested=True,
            cancel_confirmed=receipt.process_confirmed_exited,
            sync_status="in_sync" if receipt.process_confirmed_exited else "uncertain",
        )
    summary = (
        f"job {resolved} 已确认停止"
        if receipt.process_confirmed_exited
        else f"job {resolved} 取消请求已发出，但**无法确认进程已退出**"
    )
    return _result(receipt.model_dump(mode="json"), summary, resolved)


async def _request_id_belongs_to_resource(
    toolbox: Toolbox, job_id: UUID, request_id: UUID
) -> bool:
    async with toolbox.db.session() as db:
        row = (
            await db.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.session_id == toolbox.session_id,
                    ToolOperationRow.operation_id == request_id,
                )
            )
        ).scalar_one_or_none()
    return row is not None and row.job_id == job_id and row.tool_name == "cancel_job"


async def _cancel_execution_resource(
    toolbox: Toolbox, job_id: UUID, *, kind: str, tool_call_id: str
) -> str:
    """通过 `/execution/v1` 取消（持久操作身份）。终端走关闭语义。"""
    action = "terminal_close" if kind == "terminal" else "cancel_resource"
    arguments: dict[str, Any] = {"resource_id": str(job_id)}
    if kind == "terminal":
        arguments = {"terminal_id": str(job_id), "escalate": True}
    try:
        operation_id, prepared, _replayed = await prepare_execution_operation(
            toolbox,
            tool_call_id=tool_call_id,
            tool_name="cancel_job",
            action=action,
            arguments=arguments,
        )
        from agent_workbench.tools.execution_dispatch import sync_fence

        await sync_fence(toolbox)
        envelope = build_envelope(
            toolbox,
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            action=action,
            arguments=arguments,
            request_digest=str(prepared["digest"]),
            wait_ms=20_000,
        )
        receipt, failure = await dispatch_operation(toolbox, operation_id, envelope)
        if failure is not None:
            return json_text_result(failure)
        assert receipt is not None
        await project_resource(toolbox, str(receipt.get("resource_id") or job_id), receipt)
        confirmed = receipt.get("process_confirmed_exited") is True
        summary = (
            f"{kind} {job_id} 已确认停止"
            if confirmed
            else f"{kind} {job_id} 取消/关闭请求已发出，但**无法确认进程已退出**"
        )
        return _result(dict(receipt), summary, job_id)
    except Exception as exc:
        return _error_result(
            "执行岛不可达，取消请求未送达；任务可能仍在运行",
            ErrorCode.EXECUTOR_UNAVAILABLE,
            f"{type(exc).__name__}: {exc}",
            job_id,
        )


def json_text_result(result: ToolResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


@tool("export_execution_output")
async def export_execution_output(
    runtime: ToolRuntime[RunContext],
    job_id: str,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    stream: str = "stdout",
    offset: int = 0,
    limit: int = 262144,
) -> str:
    """将原始输出的一个区间保存为不可变证据工件（含非法 UTF-8 字节）。

    Args:
        job_id: 当前会话资源 ID。
        stream: stdout 或 stderr；终端仅 stdout。
        offset: 已存日志字节偏移；根据返回 next_offset 可分块导出。
        limit: 快照最多 1 MiB；hash 只对应该区间，不能代表增长中的整个文件。
    """
    import base64
    import hashlib

    toolbox = runtime.context.toolbox
    export_call_id = (
        tool_call_id
        or str(getattr(runtime, "tool_call_id", "") or "")
        or f"export-{uuid4()}"
    )
    resolved = await _resolve_job_id(toolbox, job_id)
    snapshot = await toolbox.executor.execution_raw_output(
        str(resolved), session_id=str(toolbox.session_id), stream=stream,
        offset=max(0, offset), limit=max(1, min(limit, 1048576)),
    )
    data = base64.b64decode(snapshot["data_base64"], validate=True)
    if hashlib.sha256(data).hexdigest() != snapshot["sha256"]:
        raise ExecutorProtocolError("原始日志快照 hash 不匹配")
    stored = toolbox.artifacts.write_bytes(data, media_type="application/octet-stream")
    artifact_id = uuid5(toolbox.session_id, f"execution-output:{resolved}:{stream}:{offset}:{stored.sha256}")
    captured_at = _parse_executor_time(snapshot.get("captured_at"))
    async with toolbox.db.transaction() as db:
        resource = await db.get(ExecutionResourceRow, resolved)
        origin_operation_id = resource.origin_operation_id if resource is not None else None
        origin_run_id = resource.origin_run_id if resource is not None else None
        executor_id = resource.executor_id if resource is not None else None
        repository = ArtifactRepository(db)
        if await repository.get(artifact_id) is None:
            await repository.create(
                artifact_id=artifact_id, sha256=stored.sha256, byte_size=stored.byte_size,
                media_type=stored.media_type, relative_key=stored.relative_key,
                session_id=toolbox.session_id, source_operation_id=None,
                sensitivity="internal", availability="available", captured_at=stored.captured_at,
                summary=f"资源 {resolved} {stream} 已存字节 {offset}:{snapshot['next_offset']} 的不可变快照",
                export_tool_call_id=export_call_id,
                producer_kind="execution_output",
                source_trust="ledger" if origin_operation_id is not None else "unresolved",
                resolution_status="resolved" if origin_operation_id is not None else "ambiguous",
            )
        existing_provenance = (
            await db.execute(
                select(ArtifactProvenanceRow).where(
                    ArtifactProvenanceRow.artifact_id == artifact_id,
                    ArtifactProvenanceRow.provenance_revision == 1,
                    ArtifactProvenanceRow.segment_no == 0,
                )
            )
        ).scalar_one_or_none()
        if existing_provenance is None:
            db.add(
                ArtifactProvenanceRow(
                    artifact_id=artifact_id,
                    provenance_revision=1,
                    segment_no=0,
                    export_tool_call_id=export_call_id,
                    origin_operation_id=origin_operation_id,
                    origin_run_id=origin_run_id,
                    resource_id=resolved,
                    stream=stream,
                    artifact_byte_start=0,
                    artifact_byte_end=stored.byte_size,
                    source_byte_start=max(0, offset),
                    source_byte_end=int(snapshot["next_offset"]),
                    snapshot_hash=stored.sha256,
                    identity_id=None,
                    identity_revision=None,
                    connection_origin=(
                        {"executor_id": executor_id} if executor_id is not None else {}
                    ),
                    producer_kind="execution_output",
                    source_trust="ledger" if origin_operation_id is not None else "unresolved",
                    resolution_status="resolved" if origin_operation_id is not None else "ambiguous",
                    confidence="receipt_verified" if origin_operation_id is not None else "legacy",
                    total_stream_completeness="unknown",
                    captured_at=captured_at,
                )
            )
    return _result({**{k: v for k, v in snapshot.items() if k != "data_base64"},
                    "artifact_id": str(artifact_id)}, "原始日志区间已保存为不可变工件", resolved)


def _parse_executor_time(raw: Any) -> datetime:
    """Executor 快照时间与工件登记时间分开保存；解析失败时明确回退。"""
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


JOB_TOOLS = [get_job, read_job_output, cancel_job, export_execution_output]
