"""工具派发守卫（工具入口）。

职责（§6.2、§4.3）：
- 参数校验：tool_call 的 args 必须是**完整且通过 schema 校验**的 JSON 才派发；
- 范围/能力检查：缺能力时给确定错误与替代能力；
- 操作去重（真正的去重在 `tools/operation.py`，这里做前置闸门）；
- 派发前复核：运行所有权、暂停/取消、控制版本；
- 为了让被拦截的 tool_call 组**闭合**，返回合法的终结 ToolMessage 而不是抛异常。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.run_state import is_current_owner
from agent_workbench.persistence.tables import RunRow


def _tool_message(request: Any, text: str) -> ToolMessage:
    tool_call = request.tool_call or {}
    return ToolMessage(
        content=text,
        tool_call_id=str(tool_call.get("id") or ""),
        name=str(tool_call.get("name") or "blocked_tool"),
    )


class ToolDispatchMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        context: RunContext | None = getattr(request.runtime, "context", None)
        if context is None:
            return await handler(request)

        toolbox = context.toolbox
        tool_call = request.tool_call or {}
        tool_name = str(tool_call.get("name") or "unknown")
        args = tool_call.get("args")

        # 1) 参数完整性：分片未拼完的 tool_call 参数不得执行（§11.2）
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError as exc:
                return _tool_message(
                    request,
                    f"该工具调用的参数不是完整合法的 JSON，已拒绝执行：{exc}。请重新给出完整参数。",
                )
        if args is not None and not isinstance(args, dict):
            return _tool_message(request, "工具参数必须是 JSON 对象，已拒绝执行。")
        if args is None:
            args = {}

        # 2) 能力检查：缺能力给确定错误与替代能力，不诱导模型找工具
        reason = _disabled_reason(context, tool_name)
        if reason:
            return _tool_message(request, reason)

        # 3) 所有权复核
        async with toolbox.db.session() as db:
            alive = await is_current_owner(
                db,
                run_id=toolbox.run_id,
                owner=toolbox.owner,
                generation=toolbox.owner_generation,
            )
        if not alive:
            return _tool_message(
                request,
                "本 run 的运行所有权已转移，该工具调用**未执行**。"
                "所有权变化意味着旧 owner 的响应不再派发。",
            )

        # 4) 暂停/取消：让被拦下的 tool_call 组形成合法终结结果
        async with toolbox.db.session() as db:
            run = (
                await db.execute(select(RunRow).where(RunRow.id == toolbox.run_id))
            ).scalar_one_or_none()
        if run is not None:
            status = RunStatus(run.status)
            if status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                return _tool_message(
                    request,
                    "运行已进入取消流程，该工具调用**未执行**。"
                    "已开始的工具会单独核对结果；未开始的不会开始。",
                )
            if status in {RunStatus.PAUSING, RunStatus.PAUSED}:
                return _tool_message(
                    request,
                    "运行已进入暂停流程，该工具调用**未执行**。"
                    "恢复后可以继续推进；已在运行的工具不会被杀死。",
                )

        # 5) 控制版本复核：过期模型响应中的工具不得自动执行
        dispatcher_version = int(args.get("__control_version__", run.control_version if run else 0))
        if run is not None and dispatcher_version != int(run.control_version):
            return _tool_message(
                request,
                "该工具调用来自已过期的模型响应（控制版本已变化），**未执行**。"
                "用量已记录，但不会据此产生副作用。",
            )

        # 6) 片段工作计数 + 异常转结构化结果
        #
        # 工具抛出的异常**不能**让整轮图崩溃：那会把"某个工具失败"升级成
        # "整个 run 失败"，掩盖真实原因。这里按类型区分后写成合法的终结 ToolMessage，
        # 让模型能据此换路线，也让 finish_gate 能据此判断"是否有阻塞"。
        try:
            return await handler(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _tool_message(request, _render_tool_failure(tool_name, exc))
        finally:
            toolbox.bump(f"tool:{tool_name}")
            toolbox.bump("segment_work_units")


def _disabled_reason(context: RunContext, tool_name: str) -> str | None:
    """能力开关。返回非空字符串表示拒绝原因。"""
    return context.disabled_tools.get(tool_name)


def _render_tool_failure(tool_name: str, exc: BaseException) -> str:
    """把工具异常渲染成模型可读的结构化失败。

    **区分类型**（§16.3）：参数/校验错误不是基础设施故障，
    超时与取消各有独立语义；不能统一说成"工具失败"。
    """
    import json

    name = type(exc).__name__
    message = str(exc)
    if isinstance(exc, ValueError):
        code, retryable, hint = (
            "validation_failed",
            False,
            "参数不合法，重试同样会失败；请修正参数或改用其它工具。",
        )
    elif isinstance(exc, TimeoutError | asyncio.TimeoutError):
        code, retryable, hint = (
            "timeout",
            True,
            "本次操作超时。若目标仍在本阶段目标内，可换用后台任务方式重试。",
        )
    else:
        code, retryable, hint = (
            "internal_error",
            False,
            "这是工具内部错误，请记录为受阻，不要谎报成功。",
        )

    return json.dumps(
        {
            "schema_version": 1,
            "tool": tool_name,
            "status": "failed",
            "error": {
                "code": code,
                "message": f"{name}: {message}",
                "retryable": retryable,
                "recovery_hint": hint,
            },
            "execution_certainty": "not_applied",
            "summary": f"工具 {tool_name} 执行失败（{code}）。",
        },
        ensure_ascii=False,
    )
