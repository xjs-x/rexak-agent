"""后台任务工具：`get_job`、`read_job_output`、`cancel_job`。

规则（§8.3）：
- `output cursor` 增量读取；**不鼓励模型密集轮询**；
- 给模型显示活动 job 简表与状态，不把所有输出反复注入；
- 取消要区分"已确认进程退出"与"状态未知"——后者不能显示为"已停止"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import ErrorCode, OperationStatus
from agent_workbench.contracts.tool import ToolError, ToolResult
from agent_workbench.execution.client import ExecutorConnectionError, ExecutorProtocolError
from agent_workbench.persistence.repositories import ToolJobRepository
from agent_workbench.tools.runtime import Toolbox

DEFAULT_OUTPUT_LIMIT = 16384


def _result(payload: dict[str, object], summary: str, job_id: UUID) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        job_id=job_id,
        status=OperationStatus.SUCCEEDED,
        summary=summary,
        data=dict(payload),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ).model_dump_json()


def _error_result(summary: str, code: ErrorCode, message: str, job_id: UUID | None) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        job_id=job_id,
        status=OperationStatus.FAILED,
        summary=summary,
        error=ToolError(code=str(code), message=message),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ).model_dump_json()


async def _resolve_job_id(toolbox: Toolbox, job_id: str) -> UUID:
    """解析 job id；支持简写（例如只给前 8 位）以减少模型输出错误。"""
    candidate = job_id.strip()
    try:
        return UUID(candidate)
    except ValueError:
        pass
    async with toolbox.db.session() as db:
        jobs = await ToolJobRepository(db).list_active(toolbox.settings.executor_id)
    matches = [row for row in jobs if str(row.job_id).startswith(candidate)]
    if len(matches) == 1:
        return matches[0].job_id
    if not matches:
        raise ValueError(
            f"找不到 job：{job_id}。可从 http_request 的返回或活动任务列表获取完整 job_id。"
        )
    raise ValueError(f"job 前缀匹配到 {len(matches)} 条，请给出更完整的 job_id")


@tool("get_job")
async def get_job(runtime: ToolRuntime[RunContext], job_id: str) -> str:
    """查询一个后台任务的状态（**不做密集轮询**；有终态事件会通知你）。

    Args:
        job_id: 任务 ID；可用前 8 位简写。

    Returns:
        JobSnapshot 的 JSON。`state=unknown` 表示无法确认终态，需要人工核对，
        不要自动重做该操作。
    """
    toolbox: Toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_job_id(toolbox, job_id)
    except ValueError as exc:
        return _error_result("无法解析 job_id", ErrorCode.VALIDATION_FAILED, str(exc), None)

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

    payload = snapshot.model_dump(mode="json")
    return _result(payload, f"job {resolved} 当前状态：{snapshot.state}", resolved)


@tool("read_job_output")
async def read_job_output(
    runtime: ToolRuntime[RunContext],
    job_id: str,
    cursor: str | None = None,
    stream: str = "stdout",
    limit: int = DEFAULT_OUTPUT_LIMIT,
) -> str:
    """**增量**读取后台任务的输出。

    Args:
        job_id: 任务 ID；可用前 8 位简写。
        cursor: 上次返回的 next_cursor；不传表示从头读。
        stream: stdout 或 stderr。
        limit: 本次最多读取的字节数（默认 16 KiB）。

    Returns:
        OutputPage 的 JSON：text 是本段输出，next_cursor 用于下次继续读，eof 表示读到底。

    Notes:
        - 输出可能很大，请用 cursor 分段读取，不要把全部日志一次读进上下文；
        - 输出不是合法 UTF-8 时会给出明确说明，原始字节仍然保留在工件里。
    """
    toolbox: Toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_job_id(toolbox, job_id)
    except ValueError as exc:
        return _error_result("无法解析 job_id", ErrorCode.VALIDATION_FAILED, str(exc), None)

    try:
        page = await toolbox.executor.read_output(
            resolved, cursor, stream=stream, limit=max(1, min(int(limit), 262144))
        )
    except ExecutorConnectionError as exc:
        return _error_result(
            "执行岛不可达，无法读取输出", ErrorCode.EXECUTOR_UNAVAILABLE, str(exc), resolved
        )
    except ExecutorProtocolError as exc:
        return _error_result(
            "执行岛返回了不符合协议的数据", ErrorCode.EXECUTOR_INCOMPATIBLE, str(exc), resolved
        )

    return _result(page.model_dump(mode="json"), f"读取到 {page.byte_count} 字节输出", resolved)


@tool("cancel_job")
async def cancel_job(runtime: ToolRuntime[RunContext], job_id: str, request_id: str) -> str:
    """取消一个后台任务（幂等）。

    Args:
        job_id: 任务 ID；可用前 8 位简写。
        request_id: 幂等键，同一次取消动作重复调用请使用同一个值。

    Returns:
        CancelReceipt 的 JSON。**重点看 `process_confirmed_exited`**：
        为 false 时必须向用户显示"状态未知/取消未确认"，不能显示"已停止"。
    """
    toolbox: Toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_job_id(toolbox, job_id)
        parsed_request = UUID(request_id)
    except ValueError as exc:
        return _error_result("参数非法", ErrorCode.VALIDATION_FAILED, str(exc), None)

    try:
        receipt = await toolbox.executor.cancel(resolved, parsed_request)
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


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


JOB_TOOLS = [get_job, read_job_output, cancel_job]
