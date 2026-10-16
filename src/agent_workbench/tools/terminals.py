"""PTY 终端工具：`shell_open`、`shell_send`、`shell_read`、`shell_resize`、`shell_list`、`shell_close`。

四模式（写进工具说明，模型可见）：
- interactive：有界等首次可读输出/提示符/静默后返回，进程继续运行；
- hands_free：创建后立即返回，之后显式读取或等待事件；
- dispatch：创建后返回，首个等待条件满足时记录一次通知（通知不是"进程已成功退出"）；
- monitor：创建后返回，持续匹配 watch 规则；是否唤醒由**有效委托**决定。

输入构造顺序固定：text -> submit 回车 -> keys -> hex -> paste（在写入前整体校验）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import ErrorCode, OperationStatus
from agent_workbench.contracts.tool import ToolResult
from agent_workbench.persistence.tables import ExecutionResourceRow
from agent_workbench.tools.execution_dispatch import (
    ExecutionUnavailable,
    build_envelope,
    dispatch_operation,
    failure_result,
    json_text,
    load_capabilities,
    prepare_execution_operation,
    project_resource,
    require_capability,
    sync_fence,
)
from agent_workbench.tools.runtime import Toolbox

_terminal_modes = ("interactive", "hands_free", "dispatch", "monitor")


def _now() -> datetime:
    return datetime.now(UTC)


async def _resolve_terminal(toolbox: Toolbox, terminal_id: str) -> str:
    """按会话解析终端 ID，支持前缀简写；跨会话一律拒绝。"""
    candidate = (terminal_id or "").strip()
    if not candidate:
        raise ValueError("terminal_id 不得为空")
    try:
        parsed = UUID(candidate)
        full = str(parsed)
    except ValueError:
        parsed = None
        full = ""
    async with toolbox.db.session() as db:
        rows = list(
            (
                await db.execute(
                    select(ExecutionResourceRow).where(
                        ExecutionResourceRow.session_id == toolbox.session_id,
                        ExecutionResourceRow.kind == "terminal",
                    )
                )
            )
            .scalars()
            .all()
        )
    if parsed is not None:
        for row in rows:
            if str(row.resource_id) == full:
                return full
        # 归属对不上：不作为"可管理资源"，但也明确这是跨会话或不存在
        raise ValueError(f"当前会话中没有该终端：{candidate}")
    matches = [str(row.resource_id) for row in rows if str(row.resource_id).startswith(candidate)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"找不到终端：{candidate}。可用 shell_list 查看当前会话的终端完整 ID。"
        )
    raise ValueError(f"终端前缀匹配到 {len(matches)} 条，请给出更完整的 terminal_id")


async def _read_resource_page(
    toolbox: Toolbox,
    *,
    resource_id: str,
    stream: str,
    cursor: str | None,
    limit: int,
    view: str,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """读取资源输出/屏幕。**读操作不产生副作用**，但仍核对会话归属。"""
    await _resolve_terminal(toolbox, resource_id)
    return await toolbox.executor.execution_output(
        resource_id,
        stream=stream,
        cursor=cursor,
        limit=max(1, min(int(limit), 262144)),
        view=view,
        max_lines=max_lines,
        session_id=str(toolbox.session_id),
    )


async def _terminals_tool(
    toolbox: Toolbox,
    *,
    tool_call_id: str,
    tool_name: str,
    capability: str,
    action: str,
    arguments: dict[str, Any],
    wait_ms: int = 0,
) -> ToolResult:
    operation_id: UUID | None = None
    try:
        caps = await load_capabilities(toolbox)
        require_capability(caps, capability)
        await toolbox.assert_control_current()
        operation_id, prepared, _replayed = await prepare_execution_operation(
            toolbox,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            action=action,
            arguments=arguments,
        )
        await sync_fence(toolbox)
        envelope = build_envelope(
            toolbox,
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            action=action,
            arguments=arguments,
            request_digest=str(prepared["digest"]),
            wait_ms=wait_ms,
        )
        receipt, failure = await dispatch_operation(toolbox, operation_id, envelope)
        if failure is not None:
            return failure
        assert receipt is not None
        resource_id = str(receipt.get("terminal_id") or receipt.get("resource_id") or "")
        if resource_id:
            await project_resource(toolbox, resource_id, receipt)
        return _receipt_result(operation_id, receipt)
    except ExecutionUnavailable as exc:
        return failure_result(operation_id, exc.code, exc.message, hint=exc.hint)
    except ValueError as exc:
        return failure_result(operation_id, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")


def _receipt_result(operation_id: UUID, receipt: dict[str, Any]) -> ToolResult:
    state = str(receipt.get("process_state") or "running")
    status = OperationStatus.SUCCEEDED
    if state == "unknown":
        status = OperationStatus.UNKNOWN
    elif state in {"exited", "closed"}:
        status = OperationStatus.SUCCEEDED
    elif receipt.get("status") in {"unknown"}:
        status = OperationStatus.UNKNOWN
    summary = str(receipt.get("note") or "")
    if not summary:
        summary = f"终端操作完成（{receipt.get('action')}）；状态 {state}"
    return ToolResult(
        operation_id=operation_id,
        job_id=_uuid(receipt.get("resource_id")),
        status=status,
        summary=summary,
        data=dict(receipt),
        started_at=_now(),
        finished_at=_now(),
    )


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


@tool("shell_open")
async def shell_open(
    runtime: ToolRuntime[RunContext],
    command: str | None = None,
    argv: list[str] | None = None,
    cwd: str = ".",
    env: dict[str, str] | None = None,
    mode: Literal["interactive", "hands_free", "dispatch", "monitor"] = "interactive",
    rows: int = 24,
    cols: int = 80,
    prompt_regex: str | None = None,
    quiet_ms: int = 8000,
    startup_grace_ms: int = 15000,
    watch_json: list[dict[str, Any]] | str | None = None,
    description: str = "",
) -> str:
    """打开一个**持久终端**（PTY）；需要连续输入、目录/环境延续时使用。

    Args:
        command / argv: 二选一；shell 文本或参数数组。
        cwd / env: 工作目录与环境变量；持久终端会保持自己的状态。
        mode: 四种模式——interactive（等首次输出/提示符/静默后返回）、
            hands_free（立即返回）、dispatch（首个等待条件满足时记一次通知）、
            monitor（持续匹配 watch，命中不关闭资源）。
        rows / cols: 终端尺寸（默认 24×80）。
        prompt_regex: 提示符正则（观察信号，**不是**退出码）。
        quiet_ms / startup_grace_ms: 静默判定与启动宽限。
        watch_json: watch 规则（monitor 模式常用）。
        description: 用途说明。

    Returns:
        ToolResult JSON，含 terminal_id、process_state、interaction_state、
        首次等待的 wait_reason。prompt/quiet 只表示观察到的输出边界。

    Notes:
        - 打开终端**不代表**获得持续处理授权；需要持续处理请另行建立值班委托；
        - Executor 重启后交互会丢失：历史日志保留，不自动重建、不自动重发输入；
        - 关闭必须显式调用 shell_close；读取不会关闭终端。
    """
    toolbox = runtime.context.toolbox
    try:
        if bool(command) == bool(argv):
            raise ValueError("command 与 argv 必须二选一")
        rules = watch_json
        if isinstance(rules, str):
            rules = json.loads(rules)
        if rules is not None and not isinstance(rules, list):
            raise ValueError("watch_json 必须是规则数组")
        if int(rows) <= 0 or int(cols) <= 0:
            raise ValueError("rows/cols 必须是正数")
        arguments: dict[str, Any] = {
            "command": command,
            "argv": argv,
            "cwd": cwd or ".",
            "env": {str(k): str(v) for k, v in (env or {}).items()},
            "mode": mode,
            "rows": int(rows),
            "cols": int(cols),
            "prompt_regex": prompt_regex,
            "quiet_ms": int(quiet_ms),
            "startup_grace_ms": int(startup_grace_ms),
            "watch": list(rules or []),
            "description": description or "",
            "workspace_id": str(toolbox.session_id),
        }
    except (ValueError, json.JSONDecodeError) as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await _terminals_tool(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="shell_open",
        capability="pty",
        action="terminal_open",
        arguments=arguments,
    )
    return json_text(result)


@tool("shell_send")
async def shell_send(
    runtime: ToolRuntime[RunContext],
    terminal_id: str,
    text: str | None = None,
    submit: bool = False,
    keys: list[str] | None = None,
    hex_bytes: list[str] | None = None,
    paste: str | None = None,
    wait: bool = True,
    wait_ms: int = 10000,
    prompt_regex: str | None = None,
    quiet_ms: int | None = None,
) -> str:
    """向持久终端发送输入。构造顺序固定：text -> submit 回车 -> keys -> hex -> paste。

    Args:
        terminal_id: shell_open 返回的终端 ID（支持前缀简写）。
        text: 要输入的文本（保留首尾空格与控制字符）。
        submit: 是否在 text 之后发送回车。
        keys: 按键列表，支持 Enter/Tab/Esc/Backspace/Delete/方向键/Home/End/
            PageUp/PageDown/Ctrl-C/Ctrl-D。
        hex_bytes: 原始字节（十六进制字符串列表，例如 ["03","1b5b41"]）。显式指定，不静默转码。
        paste: 按 bracketed-paste 协议粘贴的文本；**目标程序可能不支持**，不承诺绝不执行。
        wait: 是否等待本次输入之后的输出边界（默认 true）。
        wait_ms: 等待上限毫秒（默认 10000）。
        prompt_regex / quiet_ms: 覆盖该终端的等待信号。

    Returns:
        ToolResult JSON：input_seq（服务端持久分配）、written_bytes、wait_reason
        （output/prompt/quiet/exited/wait_timeout）、以及本次输入之后的输出页。

    Notes:
        - 任何参数非法时**整个输入都不会被发送**；
        - 同一 operation 重试返回原回执，不会重复写入；
        - 等待到期只结束本次等待，不关闭终端。
    """
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_terminal(toolbox, terminal_id)
        arguments: dict[str, Any] = {
            "terminal_id": resolved,
            "text": text,
            "submit": bool(submit),
            "keys": [str(item) for item in (keys or [])],
            "hex": [str(item) for item in (hex_bytes or [])],
            "paste": paste,
            "wait": bool(wait),
            "wait_ms": max(0, min(int(wait_ms), 60000)),
            "prompt_regex": prompt_regex,
            "quiet_ms": int(quiet_ms) if quiet_ms is not None else None,
        }
    except ValueError as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await _terminals_tool(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="shell_send",
        capability="pty",
        action="terminal_send",
        arguments=arguments,
    )
    return json_text(result)


@tool("shell_read")
async def shell_read(
    runtime: ToolRuntime[RunContext],
    terminal_id: str,
    view: Literal["stream", "screen"] = "stream",
    cursor: str | None = None,
    max_bytes: int = 16384,
    max_lines: int = 200,
) -> str:
    """读取终端输出流或屏幕快照（有界；读操作不产生副作用）。

    Args:
        terminal_id: 终端 ID。
        view: `stream`（增量输出，按 cursor 分页）或 `screen`（当前屏幕与光标）。
        cursor: 上次返回的 next_cursor；不传从头读。
        max_bytes / max_lines: 单页上限。

    Returns:
        OutputPageV2 JSON。PTY 输出为合流（stdout/stderr 不可分离）；
        重启后的屏幕会标注 rebuilt=true 并说明来源。
    """
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_terminal(toolbox, terminal_id)
        page = await _read_resource_page(
            toolbox,
            resource_id=resolved,
            stream="stdout",
            cursor=cursor,
            limit=max(1, min(int(max_bytes), 262144)),
            view=view,
            max_lines=max(1, min(int(max_lines), 5000)),
        )
    except ValueError as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    except ExecutionUnavailable as exc:
        return json_text(failure_result(None, exc.code, exc.message, hint=exc.hint))
    return json_text(
        ToolResult(
            operation_id=UUID(int=0),
            job_id=_uuid(resolved),
            status=OperationStatus.SUCCEEDED,
            summary=f"读取终端输出（view={view}，{page.get('byte_count', 0)} 字节）",
            data=dict(page),
            started_at=_now(),
            finished_at=_now(),
        )
    )


@tool("shell_resize")
async def shell_resize(
    runtime: ToolRuntime[RunContext],
    terminal_id: str,
    rows: int,
    cols: int,
) -> str:
    """调整终端窗口尺寸（原子调整 PTY 与终端模拟器，并记录操作）。"""
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_terminal(toolbox, terminal_id)
        if int(rows) <= 0 or int(cols) <= 0:
            raise ValueError("rows/cols 必须是正数")
        arguments = {"terminal_id": resolved, "rows": int(rows), "cols": int(cols)}
    except ValueError as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await _terminals_tool(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="shell_resize",
        capability="pty",
        action="terminal_resize",
        arguments=arguments,
    )
    return json_text(result)


@tool("shell_list")
async def shell_list(
    runtime: ToolRuntime[RunContext],
    limit: int = 50,
    offset: int = 0,
) -> str:
    """列出当前会话的终端（分页）：包含交互是否仍可用与状态。

    Returns:
        ToolResult JSON，`data.resources` 为终端状态列表；
        已退出的终端记录仍可查。
    """
    toolbox = runtime.context.toolbox
    try:
        await load_capabilities(toolbox)
        page = await toolbox.executor.execution_resources(
            session_id=str(toolbox.session_id),
            limit=max(1, min(int(limit), 200)),
            offset=max(0, int(offset)),
            kind="terminal",
        )
    except ExecutionUnavailable as exc:
        return json_text(failure_result(None, exc.code, exc.message, hint=exc.hint))
    except Exception as exc:
        return json_text(
            failure_result(
                None,
                str(ErrorCode.EXECUTOR_UNAVAILABLE),
                f"执行岛不可达：{type(exc).__name__}",
            )
        )
    return json_text(
        ToolResult(
            operation_id=UUID(int=0),
            status=OperationStatus.SUCCEEDED,
            summary=f"当前会话终端 {len(page.get('resources') or [])} 个",
            data=dict(page),
            started_at=_now(),
            finished_at=_now(),
        )
    )


@tool("shell_close")
async def shell_close(
    runtime: ToolRuntime[RunContext],
    terminal_id: str,
    escalate: bool = True,
) -> str:
    """关闭终端：正常终止并必要时升级；保留日志与回执；重复调用不再产生副作用。

    Args:
        terminal_id: 终端 ID。
        escalate: 正常终止未确认时是否升级为强制结束。

    Returns:
        ToolResult JSON。**status=unknown 时表示取消未确认**，UI/模型都必须这样展示，
        不能显示成"已停止"。关闭**不影响**其他资源与委托记录。
    """
    toolbox = runtime.context.toolbox
    try:
        resolved = await _resolve_terminal(toolbox, terminal_id)
        arguments = {"terminal_id": resolved, "escalate": bool(escalate)}
    except ValueError as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await _terminals_tool(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="shell_close",
        capability="pty",
        action="terminal_close",
        arguments=arguments,
    )
    if result.status is OperationStatus.UNKNOWN:
        result.summary = (
            "关闭请求已发出，但**无法确认进程已退出**；按 unknown 处理，"
            "不得显示为「已停止」。"
        )
    return json_text(result)


TERMINAL_TOOLS = [shell_open, shell_send, shell_read, shell_resize, shell_list, shell_close]
