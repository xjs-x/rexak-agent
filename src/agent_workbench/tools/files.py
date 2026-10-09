"""任务工作区文件工具：`list_files`、`read_file`、`write_file`、`edit_file`。

与 HTTP 工具共享同一套远端执行骨架（`tools/remote.py`），因此同样具备：
稳定 `operation_id`、幂等接收、统一 `ToolResult`、超过交互阈值返回 `accepted`。

边界（§8.2、§9.1）：
- 只在**本任务工作区**内操作；客户端只能传 `workspace_id`（= session id），
  路径由 Executor 解析并拒绝越界与软链接逃逸；
- **写操作**不会静默覆盖：`write_file` 对已存在文件必须显式给出 `expected_version`，
  `edit_file` 必须给 `expected_version` 且匹配必须唯一（除非显式 `replace_all`）；
- 文件工具**不受**"通用命令无人值守模式关闭"影响 —— 那个限制针对任意 argv 与
  出站网络，不是文件访问。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import (
    ErrorCode,
    ExecutionCertainty,
    OperationStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.tool import ToolError, ToolResult
from agent_workbench.execution.client import ExecutorRejected
from agent_workbench.tools.remote import (
    download_result_json,
    json_text,
    prepare_operation,
    record_result,
    record_submitted,
    rejection_result,
    transport_unknown_result,
    wait_or_accept,
)
from agent_workbench.tools.runtime import Toolbox

_ACCEPT_NOTE = (
    "文件操作已受理但尚未结束（超过交互等待阈值）。稍后用 get_job 查询状态；"
    "**不要**重复提交同一个操作。"
)


def _failed(
    operation_id: UUID, code: ErrorCode, message: str, hint: str | None = None
) -> ToolResult:
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.FAILED,
        summary=message,
        error=ToolError(code=str(code), message=message, recovery_hint=hint),
        execution_certainty=ExecutionCertainty.NOT_APPLIED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


async def run_filesystem_action(
    toolbox: Toolbox,
    *,
    tool_call_id: str,
    tool_name: str,
    action: Literal["list", "read", "write", "edit"],
    path: str = "",
    content: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    expected_version: str | None = None,
    replace_all: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> ToolResult:
    # 写操作属于副作用；读/列目录是只读
    effect_class = (
        ToolEffectClass.SIDE_EFFECTING if action in {"write", "edit"} else ToolEffectClass.READ_ONLY
    )

    await toolbox.assert_control_current()

    # 同一工作区内的写操作串行，避免两个 edit 互相覆盖
    keys = (
        [f"workspace:{toolbox.session_id}"]
        if effect_class is ToolEffectClass.SIDE_EFFECTING
        else []
    )

    async with toolbox.locks.acquire(keys, timeout=60.0):
        arguments: dict[str, Any] = {
            "action": action,
            "path": path,
            "workspace_id": str(toolbox.session_id),
        }
        operation_id, _replayed = await prepare_operation(
            toolbox, tool_call_id, tool_name, arguments, effect_class=effect_class
        )

        operation = {
            "operation_id": str(operation_id),
            "kind": "filesystem",
            "request_digest": _digest(arguments, content, old_text, new_text, expected_version),
            "scope_revision": toolbox.scope_revision,
            "owner_generation": toolbox.owner_generation,
            # 文件操作不联网，范围规则对它无意义；仍带上以满足协议字段
            "scope": {"revision": toolbox.scope_revision, "rules": [], "mappings": []},
            "payload": {
                "action": action,
                "workspace_id": str(toolbox.session_id),
                "path": path,
                "content": content,
                "old_text": old_text,
                "new_text": new_text,
                "expected_version": expected_version,
                "replace_all": replace_all,
                "offset": offset,
                "limit": limit,
            },
        }

        try:
            receipt = await toolbox.executor.submit(operation)
        except ExecutorRejected as exc:
            await record_result(
                toolbox,
                operation_id=operation_id,
                job_id=None,
                status=str(OperationStatus.FAILED),
                failure=f"被执行岛拒绝：{exc}",
            )
            return rejection_result(operation_id, exc)
        except Exception as exc:
            return transport_unknown_result(operation_id, exc)

        await record_submitted(toolbox, operation_id, receipt.job_id)

        accepted = await wait_or_accept(
            toolbox, operation_id, receipt.job_id, accept_note=_ACCEPT_NOTE
        )
        if accepted is not None:
            return accepted

    snapshot = await toolbox.executor.get_job(receipt.job_id)
    payload = await download_result_json(toolbox, receipt.job_id)
    state = str(snapshot.state)

    if state != "succeeded":
        message = f"文件操作未成功：{state}" + (
            f"；{snapshot.error.message}" if snapshot.error else ""
        )
        await record_result(
            toolbox,
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=state,
        )
        return ToolResult(
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=OperationStatus.FAILED,
            summary=message,
            error=ToolError(
                code=(snapshot.error.code if snapshot.error else str(ErrorCode.INTERNAL_ERROR)),
                message=message,
                retryable=False,
                recovery_hint="这是被拒绝或被拦截的操作，重试同样会失败；请修正参数或先读后改。",
            ),
            execution_certainty=ExecutionCertainty.NOT_APPLIED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    if payload is None:
        # 成功但拿不到结构化结果：**不能**当成功处理
        await record_result(
            toolbox,
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=str(OperationStatus.UNKNOWN),
        )
        return ToolResult(
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=OperationStatus.UNKNOWN,
            summary="执行岛报告成功，但结果工件无法取回；无法确认实际效果",
            error=ToolError(
                code=str(ErrorCode.EXECUTOR_UNAVAILABLE),
                message="result.json 缺失或无法解析",
                retryable=False,
                recovery_hint="用 get_job 查看状态与输出，不要直接重复执行写操作。",
            ),
            execution_certainty=ExecutionCertainty.UNCERTAIN,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    error = payload.get("error") or {}
    result = payload.get("result") or {}
    await record_result(
        toolbox,
        operation_id=operation_id,
        job_id=receipt.job_id,
        status=(str(OperationStatus.FAILED) if error else str(OperationStatus.SUCCEEDED)),
    )

    if error:
        return ToolResult(
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=OperationStatus.FAILED,
            summary=f"{action} 被拒绝：{error.get('message', '')}",
            data={"error_code": error.get("code")},
            error=ToolError(
                code=str(ErrorCode.VALIDATION_FAILED),
                message=str(error.get("message", "")),
                retryable=False,
                recovery_hint="工作区路径或版本校验未通过；重试同样会失败。",
            ),
            execution_certainty=ExecutionCertainty.NOT_APPLIED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    return ToolResult(
        operation_id=operation_id,
        job_id=receipt.job_id,
        status=OperationStatus.SUCCEEDED,
        summary=_summarize(action, result),
        data=result,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


def _summarize(action: str, result: dict[str, Any]) -> str:
    if action == "list":
        return f"目录 {result.get('path')} 下有 {result.get('count', 0)} 个条目。"
    if action == "read":
        note = result.get("encoding_note")
        base = (
            f"读取 {result.get('path')}（{result.get('byte_size')} 字节，"
            f"version={str(result.get('version'))[:12]}…）"
        )
        return f"{base}；{note}" if note else base
    if action == "write":
        return (
            f"已写入 {result.get('path')}（{result.get('byte_size')} 字节，"
            f"version={str(result.get('version'))[:12]}…）"
        )
    return (
        f"已编辑 {result.get('path')}（替换 {result.get('replacements')} 处，"
        f"version={str(result.get('version'))[:12]}…）"
    )


def _digest(
    arguments: dict[str, Any],
    content: str | None,
    old_text: str | None,
    new_text: str | None,
    expected_version: str | None,
) -> str:
    from agent_workbench.persistence.repositories import compute_request_digest

    return compute_request_digest(
        {
            "tool": "filesystem",
            "arguments": arguments,
            "content": content,
            "old_text": old_text,
            "new_text": new_text,
            "expected_version": expected_version,
        }
    )


# --------------------------------------------------------------------------- #
# 对外工具
# --------------------------------------------------------------------------- #


@tool("list_files")
async def list_files(runtime: ToolRuntime[RunContext], path: str = "") -> str:
    """列出**任务工作区**内的目录（只读，不联网）。

    Args:
        path: 工作区内的相对路径；留空表示工作区根目录。

    Returns:
        ToolResult 的 JSON，`data.entries` 含 path / kind / byte_size / modified_at。
    """
    toolbox = runtime.context.toolbox
    result = await run_filesystem_action(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="list_files",
        action="list",
        path=path,
    )
    return json_text(result)


@tool("read_file")
async def read_file(
    runtime: ToolRuntime[RunContext],
    path: str,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """读取**任务工作区**内的文件（只读，不联网）。

    Args:
        path: 工作区内的相对路径。
        offset: 起始字节偏移。
        limit: 最多读取字节数（有服务端上限）。

    Returns:
        ToolResult 的 JSON，`data.text` 为内容，`data.version` 是内容 sha256 ——
        编辑前必须用它作为 `expected_version`。

    Notes:
        文件不是合法 UTF-8 时会给 `encoding_note` 而**不会**做猜测性解码。
    """
    toolbox = runtime.context.toolbox
    result = await run_filesystem_action(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="read_file",
        action="read",
        path=path,
        offset=offset,
        limit=limit,
    )
    return json_text(result)


@tool("write_file")
async def write_file(
    runtime: ToolRuntime[RunContext],
    path: str,
    content: str,
    expected_version: str | None = None,
) -> str:
    """在工作区内**新建或覆盖**文件。

    Args:
        path: 工作区内的相对路径。
        content: 要写入的文本（UTF-8）。
        expected_version: 覆盖已存在文件时**必须**提供——先用 read_file 拿到当前
            `version`，再填在这里；不匹配则拒绝写入，避免盖掉别人的改动。

    Returns:
        ToolResult 的 JSON，`data.version` 是新内容的版本。
    """
    toolbox = runtime.context.toolbox
    result = await run_filesystem_action(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="write_file",
        action="write",
        path=path,
        content=content,
        expected_version=expected_version,
    )
    return json_text(result)


@tool("edit_file")
async def edit_file(
    runtime: ToolRuntime[RunContext],
    path: str,
    old_text: str,
    new_text: str,
    expected_version: str,
    replace_all: bool = False,
) -> str:
    """按内容替换编辑工作区内的文件。

    Args:
        path: 工作区内的相对路径。
        old_text: 要被替换的原文（应带足够上下文，使其唯一）。
        new_text: 替换后的文本。
        expected_version: **必填**。先用 read_file 拿到当前 `version`。
        replace_all: 是否替换全部匹配；默认 False，出现多处匹配时会拒绝。

    Returns:
        ToolResult 的 JSON，含替换处数与新版本。
    """
    toolbox = runtime.context.toolbox
    result = await run_filesystem_action(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="edit_file",
        action="edit",
        path=path,
        old_text=old_text,
        new_text=new_text,
        expected_version=expected_version,
        replace_all=replace_all,
    )
    return json_text(result)


FILE_TOOLS = [list_files, read_file, write_file, edit_file]
