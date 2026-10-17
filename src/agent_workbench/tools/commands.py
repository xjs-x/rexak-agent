"""命令与脚本工具：`run_command`、`run_script`、`start_monitor`。

行为要点（规格 §6.1）：
- 默认独立命令：`background=false` 时只等待 `wait_ms`（默认 10 秒），到期返回**原 job**，
  不取消执行、也不重新提交；
- `background=true` 接收持久化后尽快返回资源引用；
- 执行截止（`execution_timeout_seconds`）与实际等待（`wait_ms`）是两个独立概念；
- shell 文本使用固定可用 shell 的**非登录**模式；argv 直接执行、不重新拼接；
- 默认环境最小化；stdin 一次性写入后关闭；输出超限默认保活并记录缺失；
- 能力不可用/配置不可信时**确定失败**，不回退到控制端执行。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import ErrorCode, OperationStatus
from agent_workbench.contracts.tool import ToolResult
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
)
from agent_workbench.persistence.execution_projection import execution_operation_status
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

DEFAULT_WAIT_MS = 10_000
MAX_WAIT_MS = 60_000


def bounded_wait_ms(wait_ms: int | None) -> int:
    if wait_ms is None:
        return DEFAULT_WAIT_MS
    try:
        value = int(wait_ms)
    except (TypeError, ValueError):
        raise ValueError("wait_ms 必须是整数毫秒") from None
    if value < 0:
        raise ValueError("wait_ms 不得为负")
    return max(0, min(value, MAX_WAIT_MS))


def _validate_env(env: dict[str, str] | None) -> dict[str, str]:
    if not env:
        return {}
    if len(env) > 64:
        raise ValueError("env 条目过多（最多 64 条）")
    return {str(key): str(value) for key, value in env.items()}


def _result_from_receipt(operation_id: Any, receipt: dict[str, Any]) -> ToolResult:
    state = str(receipt.get("process_state") or "running")
    wait_reason = receipt.get("wait_reason")
    if state in {"exited", "closed"}:
        exit_code = receipt.get("exit_code")
        status = execution_operation_status(state, exit_code, receipt.get("termination_reason"))
        summary = (
            f"任务已结束（退出码 {exit_code}）；退出码只表示程序自身结果，"
            "不代表目标安全或任务目标达成。"
            if exit_code is not None
            else f"任务已结束（{receipt.get('termination_reason') or '原因未知'}）"
        )
        return ToolResult(
            operation_id=operation_id,
            job_id=_uuid_of(receipt.get("job_id") or receipt.get("resource_id")),
            status=OperationStatus(status),
            summary=summary,
            data=dict(receipt),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    if state == "unknown":
        return ToolResult(
            operation_id=operation_id,
            job_id=_uuid_of(receipt.get("job_id") or receipt.get("resource_id")),
            status=OperationStatus.UNKNOWN,
            summary="资源状态无法确认；按未知处理，不自动重跑",
            data=dict(receipt),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    return ToolResult(
        operation_id=operation_id,
        job_id=_uuid_of(receipt.get("job_id") or receipt.get("resource_id")),
        status=OperationStatus.ACCEPTED,
        summary=(
            f"任务受理，仍在运行（等待结果：{wait_reason or 'wait_timeout'}）。"
            "等待超时只返回同一个 job：没有取消执行，也没有第二次提交；"
            "请先查询/读取输出，不要重新提交。"
        ),
        data=dict(receipt),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


def _uuid_of(value: Any) -> Any:
    from uuid import UUID

    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


async def run_execution_kernel(
    toolbox: Toolbox,
    *,
    tool_call_id: str,
    tool_name: str,
    capability: str,
    action: str,
    arguments: dict[str, Any],
    wait_ms: int,
) -> ToolResult:
    from uuid import UUID

    operation_id: UUID | None = None
    try:
        from agent_workbench.contracts.execution_runtime import validate_arguments

        # 控制端先用**同一份协议模型**做一次前置校验：非法参数不产生操作身份、
        # 不占用配额、不派发任何副作用（执行端还会再做同样的校验）。
        validate_arguments(action, arguments)
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
        receipt["completion_role"] = arguments.get("completion_role", "resource" if action == "start_monitor" else "work")
        await _record_control_projection(toolbox, operation_id, receipt)
        await project_resource(toolbox, str(receipt.get("resource_id") or ""), receipt)
        return _result_from_receipt(operation_id, receipt)
    except ExecutionUnavailable as exc:
        result = failure_result(operation_id, exc.code, exc.message, hint=exc.hint)
    except ValueError as exc:
        result = failure_result(
            operation_id, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}"
        )
    except ExecutorProtocolError as exc:
        # 这里只覆盖派发前的能力读取、控制同步；派发后的不确定状态由 dispatch 处理。
        result = failure_result(
            operation_id,
            str(ErrorCode.EXECUTOR_INCOMPATIBLE),
            f"执行岛响应异常，未派发命令：{exc}",
            hint="请检查执行岛服务日志；这不是目标不可达，也不是命令执行结果。",
        )
    except ExecutorConnectionError as exc:
        result = failure_result(
            operation_id,
            str(ErrorCode.EXECUTOR_UNAVAILABLE),
            f"派发前连接执行岛失败：{exc}",
            hint="命令尚未派发；这是执行基础设施故障，不是目标测试结论。",
        )
    if operation_id is not None:
        from agent_workbench.persistence.repositories import ToolOperationRepository

        async with toolbox.db.transaction() as db:
            await ToolOperationRepository(db).mark_result(operation_id, status="failed")
    return result


async def _record_control_projection(
    toolbox: Toolbox, operation_id: Any, receipt: dict[str, Any]
) -> None:
    from agent_workbench.persistence.repositories import (
        ToolJobRepository,
        ToolOperationRepository,
    )

    job_id = _uuid_of(receipt.get("job_id") or receipt.get("resource_id"))
    state = str(receipt.get("process_state") or "running")
    status = execution_operation_status(
        state, receipt.get("exit_code"), receipt.get("termination_reason")
    )
    async with toolbox.db.transaction() as db:
        if job_id is not None:
            await ToolOperationRepository(db).attach_job(
                operation_id, job_id, status=status
            )
            await ToolJobRepository(db).upsert(
                job_id=job_id,
                operation_id=operation_id,
                executor_id=toolbox.settings.executor_id,
                state=status if state in {"exited", "closed"} else state,
                sync_status="in_sync",
                executor_accepted_at=datetime.now(UTC),
            )


def common_execution_arguments(
    *,
    cwd: str,
    env: dict[str, str] | None,
    stdin_text: str | None,
    execution_timeout_seconds: int | None,
    output_limit_action: str,
    description: str,
    workspace_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if execution_timeout_seconds is not None and int(execution_timeout_seconds) <= 0:
        raise ValueError("execution_timeout_seconds 必须是正数；省略表示无执行截止")
    if output_limit_action not in {"preserve_process", "terminate"}:
        raise ValueError("output_limit_action 必须是 preserve_process 或 terminate")
    payload: dict[str, Any] = {
        "cwd": cwd or ".",
        "env": _validate_env(env),
        "stdin_text": stdin_text,
        "execution_timeout_seconds": execution_timeout_seconds,
        "output_limit_action": output_limit_action,
        "description": description or "",
        "workspace_id": workspace_id,
    }
    if extra:
        payload.update(extra)
    return payload


def _arguments(
    *,
    command: str | None,
    argv: list[str] | None,
    cwd: str,
    env: dict[str, str] | None,
    stdin_text: str | None,
    execution_timeout_seconds: int | None,
    output_limit_action: str,
    description: str,
    workspace_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(command) == bool(argv):
        raise ValueError("command 与 argv 必须二选一：文本框用 command，直接执行用 argv")
    payload = common_execution_arguments(
        cwd=cwd,
        env=env,
        stdin_text=stdin_text,
        execution_timeout_seconds=execution_timeout_seconds,
        output_limit_action=output_limit_action,
        description=description,
        workspace_id=workspace_id,
        extra=extra,
    )
    payload["command"] = command
    payload["argv"] = argv
    return payload


@tool("run_command")
async def run_command(
    runtime: ToolRuntime[RunContext],
    command: str | None = None,
    argv: list[str] | None = None,
    cwd: str = ".",
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    background: bool = False,
    wait_ms: int = DEFAULT_WAIT_MS,
    execution_timeout_seconds: int | None = None,
    output_limit_action: str = "preserve_process",
    description: str = "",
    completion_role: Literal["work", "resource"] = "work",
) -> str:
    """在执行岛运行一条命令（独立命令；需要连续输入请用 shell_open 持久终端）。

    Args:
        command: shell 文本（支持管道、重定向、多行与 heredoc）。与 argv 二选一。
        argv: 参数数组，**不**经过 shell 拼接，适合带空格/特殊字符的参数。
        cwd: 工作目录（相对本会话工作区；默认工作区根）。
        env: 追加环境变量；独立命令**不继承**上一条命令的 shell 变量。
        stdin_text: 一次性写入 stdin 后关闭；省略则不接 stdin。
        background: true 时持久化受理后立即返回资源引用。
        wait_ms: 本次等待毫秒数（默认 10000，最大 60000）；**到期只返回原 job**。
        execution_timeout_seconds: 执行截止（秒）；省略表示无执行截止。
        output_limit_action: preserve_process（默认，保活并记录缺失）或 terminate。
        description: 用途说明（会记入资源台账与事件摘要）。
        completion_role: work 表示待完成工作；resource 表示保留能力（如命令启动的监听器），不阻塞阶段结束。与是否后台、是否交互独立；不改变日志保活策略。

    Returns:
        ToolResult JSON，含 resource_id/job_id、process_state、wait_reason、输出页。
        等待超时不代表失败：任务仍在运行，不要重新提交。
    """
    toolbox = runtime.context.toolbox
    try:
        bounded_wait = 0 if background else bounded_wait_ms(wait_ms)
        arguments = _arguments(
            command=command,
            argv=argv,
            cwd=cwd,
            env=env,
            stdin_text=stdin_text,
            execution_timeout_seconds=execution_timeout_seconds,
            output_limit_action=output_limit_action,
            description=description,
            workspace_id=str(toolbox.session_id),
            extra={"completion_role": completion_role},
        )
    except ValueError as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await run_execution_kernel(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="run_command",
        capability="command",
        action="run_command",
        arguments=arguments,
        wait_ms=bounded_wait,
    )
    return json_text(result)


@tool("run_script")
async def run_script(
    runtime: ToolRuntime[RunContext],
    language: Literal["python", "shell", "node"],
    code: str | None = None,
    path: str | None = None,
    params_json: str | None = None,
    cwd: str = ".",
    env: dict[str, str] | None = None,
    background: bool = False,
    wait_ms: int = DEFAULT_WAIT_MS,
    execution_timeout_seconds: int | None = None,
    output_limit_action: str = "preserve_process",
    description: str = "",
    completion_role: Literal["work", "resource"] = "work",
) -> str:
    """运行脚本（code 内联或工作区内的 path 快照），参数经 stdin JSON 传递。

    Args:
        language: python、shell 或 node。
        code: 脚本正文；与 path 二选一。会作为版本化脚本落盘后执行。
        path: 工作区内脚本路径；执行端取受理时刻的**只读快照**，不受随后修改影响。
        params_json: 参数 JSON 字符串；作为 stdin 传给脚本（不是拼进代码）。
        cwd/ env/ background/ wait_ms/ execution_timeout_seconds/ output_limit_action/ description:
            与 run_command 相同。

    Returns:
        ToolResult JSON；脚本内容哈希与输出均可追溯。
    """
    toolbox = runtime.context.toolbox
    try:
        if (code is not None) == bool(path):
            raise ValueError("code 与 path 必须二选一")
        if code is not None and len(code) > 512 * 1024:
            raise ValueError("内联脚本超过 512 KiB 上限；请写入工作区文件后用 path 运行")
        bounded_wait = 0 if background else bounded_wait_ms(wait_ms)
        arguments = common_execution_arguments(
            cwd=cwd,
            env=env,
            # 参数 JSON 经 stdin 传递，不拼成代码。
            stdin_text=params_json,
            execution_timeout_seconds=execution_timeout_seconds,
            output_limit_action=output_limit_action,
            description=description,
            workspace_id=str(toolbox.session_id),
            extra={"language": language, "code": code, "path": path, "params_json": params_json,
                   "completion_role": completion_role},
        )
    except ValueError as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await run_execution_kernel(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="run_script",
        capability="script",
        action="run_script",
        arguments=arguments,
        wait_ms=bounded_wait,
    )
    return json_text(result)


@tool("start_monitor")
async def start_monitor(
    runtime: ToolRuntime[RunContext],
    command: str | None = None,
    argv: list[str] | None = None,
    watch_json: list[dict[str, Any]] | str | None = None,
    cwd: str = ".",
    env: dict[str, str] | None = None,
    description: str = "",
    execution_timeout_seconds: int | None = None,
    output_limit_action: str = "preserve_process",
) -> str:
    """启动长期监视任务：默认后台，`watch` 命中/退出/失败产生事件。

    Args:
        command / argv: 与 run_command 相同（二选一）。
        watch_json: watch 规则数组，例如
            `[{"kind":"regex","pattern":"login successful","notify":"once"}]`。
        description: 必须在描述里写清监视目的。
        execution_timeout_seconds: 缺省为 null（无执行截止）。
        output_limit_action: 默认 preserve_process。

    Returns:
        ToolResult JSON。**创建监视不等于获得持续处理授权**：
        事件会被记录，但阶段结束后不会自动唤醒模型；需要持续处理请另行建立值班委托。

    Notes:
        命中通知不是"程序已成功退出"；监视资源不隐式阻塞阶段完成。
    """
    toolbox = runtime.context.toolbox
    try:
        rules = watch_json
        if isinstance(rules, str):
            rules = json.loads(rules)
        if rules is not None and not isinstance(rules, list):
            raise ValueError("watch_json 必须是规则数组")
        arguments = _arguments(
            command=command,
            argv=argv,
            cwd=cwd,
            env=env,
            stdin_text=None,
            execution_timeout_seconds=execution_timeout_seconds,
            output_limit_action=output_limit_action,
            description=description,
            workspace_id=str(toolbox.session_id),
            extra={"watch": list(rules or []), "completion_role": "resource"},
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return json_text(
            failure_result(None, str(ErrorCode.VALIDATION_FAILED), f"参数非法：{exc}")
        )
    result = await run_execution_kernel(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="start_monitor",
        capability="monitor",
        action="start_monitor",
        arguments=arguments,
        wait_ms=0,
    )
    return json_text(result)


COMMAND_TOOLS = [run_command, run_script, start_monitor]
