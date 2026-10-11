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

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.signals import StaleResponseError
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import is_current_owner
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.tables import RunRow, ToolOperationRow

log = structlog.get_logger(__name__)


def _tool_message(request: Any, text: str) -> ToolMessage:
    tool_call = request.tool_call or {}
    return ToolMessage(
        content=text,
        tool_call_id=str(tool_call.get("id") or ""),
        name=str(tool_call.get("name") or "blocked_tool"),
        status="error",
    )


class ToolDispatchMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        from agent_workbench.observability.tool_activity import emit_tool_activity, result_view

        context = getattr(request.runtime, "context", None)
        if context is None:
            return await handler(request)
        call = request.tool_call or {}

        async def emit(phase: str, status: str, output: str = "") -> None:
            try:
                await emit_tool_activity(
                    context.toolbox, call, phase=phase, status=status, output=output
                )
            except Exception as exc:
                # 展示故障不能把已执行的操作变成未执行；记录错误，不覆盖工具结果。
                log.warning("tool_activity_event_failed", error_type=type(exc).__name__)

        await emit("started", "preparing")
        try:
            result = await self._dispatch_tool_call(request, handler)
        except asyncio.CancelledError:
            await emit(
                "interrupted", "interrupted", "Agent 已打断等待；远端任务状态须以执行台账为准。"
            )
            raise
        except Exception as exc:
            await emit("finished", "failed", f"工具派发检查失败：{type(exc).__name__}")
            raise
        state, output = result_view(result)
        await emit("finished", state, output)
        return result

    async def _dispatch_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
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

        # 5) 控制版本复核：**过期模型响应中的工具不得自动执行**（§4.3）
        #
        # 比较的两个值必须是"两个不同时刻的版本"：
        #   - pinned：本模型轮次开始前，由 CommandGuardMiddleware 固定下来的版本；
        #   - current：现在数据库里的版本。
        # 之前的实现把"模型参数里自填的值"或"数据库当前值"当作 pinned，
        # 等于拿当前值跟当前值比，永远相等 —— 用户改变方向拦不住旧响应。
        pinned = request.state.get("dispatch_control_version") if request.state else None
        current = int(run.control_version) if run is not None else 0

        if pinned is None:
            # fail-closed：无法确认"这一轮是什么版本"，就不能派发。
            # 这只可能发生在中间件装配错误时，必须显式暴露而不是放行。
            await self._emit_stale_block(toolbox, tool_name, None, current)
            return _tool_message(
                request,
                "无法确认该工具调用所属的模型轮次版本（dispatch pinning 未生效），"
                "为安全起见**未执行**。这是实现配置问题，请报告给用户。",
            )

        if int(pinned) != current:
            await self._emit_stale_block(toolbox, tool_name, int(pinned), current)
            return _tool_message(
                request,
                f"该工具调用来自**已过期的模型响应**（模型请求发出时 control_version="
                f"{pinned}，当前为 {current}），**未执行**。"
                "用户在此期间改变了方向；请基于最新状态重新决策。"
                "该次模型调用的用量仍会被记录。",
            )

        # 6) 片段工作计数 + 异常转结构化结果
        #
        # 工具抛出的异常**不能**让整轮图崩溃：那会把"某个工具失败"升级成
        # "整个 run 失败"，掩盖真实原因。这里按类型区分后写成合法的终结 ToolMessage，
        # 让模型能据此换路线，也让 finish_gate 能据此判断"是否有阻塞"。
        try:
            result = await handler(request)
            try:
                async with toolbox.db.session() as db:
                    after = (
                        await db.execute(
                            select(RunRow.control_version).where(RunRow.id == toolbox.run_id)
                        )
                    ).scalar_one()
            except Exception as exc:
                return _tool_message(
                    request,
                    "工具调用可能已经产生远端副作用，但结果返回后无法核对控制版本"
                    f"（{type(exc).__name__}）。结果未注入当前决策；"
                    "请先按既有 operation_id/job_id 核对，不能直接重试。",
                )
            if int(pinned) != int(after):
                reference_error = False
                try:
                    async with toolbox.db.session() as db:
                        operation = (
                            await db.execute(
                                select(ToolOperationRow).where(
                                    ToolOperationRow.run_id == toolbox.run_id,
                                    ToolOperationRow.tool_call_id == str(tool_call.get("id") or ""),
                                )
                            )
                        ).scalar_one_or_none()
                except Exception:
                    operation = None
                    reference_error = True
                event_error = False
                try:
                    await self._emit_stale_result(
                        toolbox,
                        tool_name,
                        int(pinned),
                        int(after),
                        operation,
                    )
                except Exception:
                    event_error = True
                reference = (
                    f"operation_id={operation.operation_id}，job_id={operation.job_id}。"
                    if operation is not None
                    else "请核对该工具的执行记录。"
                )
                return _tool_message(
                    request,
                    "工具调用已开始后用户改变了方向；远端副作用可能已发生，"
                    "本次结果未注入当前决策。"
                    f"{reference}请按当前目标重新核对，不能把旧结果直接当成当前结论。"
                    + (
                        "执行记录或事件核对失败，请检查数据库连接。"
                        if event_error or reference_error
                        else ""
                    ),
                )
            return result
        except asyncio.CancelledError:
            raise
        except StaleResponseError as exc:
            # 工具内部（例如 HTTP 工具在提交到执行岛之前）自查发现响应已过期。
            # 与上面的前置检查同义：**未执行**，并留下事件。
            await self._emit_stale_block(
                toolbox, tool_name, exc.pinned_version, exc.current_version
            )
            return _tool_message(
                request,
                f"该工具调用来自**已过期的模型响应**（固定版本 {exc.pinned_version}，"
                f"当前 {exc.current_version}），**未执行**。"
                "用户在此期间改变了方向；请基于最新状态重新决策。",
            )
        except Exception as exc:
            return _tool_message(request, _render_tool_failure(tool_name, exc))
        finally:
            toolbox.bump(f"tool:{tool_name}")
            toolbox.bump("segment_work_units")

    @staticmethod
    async def _emit_stale_block(
        toolbox: Any, tool_name: str, pinned: int | None, current: int
    ) -> None:
        """把"拦下过期响应"写进事件流，便于 UI 与诊断复盘。"""
        try:
            async with toolbox.db.transaction() as db:
                await append_event(
                    db,
                    session_id=toolbox.session_id,
                    run_id=toolbox.run_id,
                    event_type=EventType.ERROR,
                    payload={
                        "scope": "stale_response_blocked",
                        "tool": tool_name,
                        "pinned_control_version": pinned,
                        "current_control_version": current,
                        "message": (
                            "该工具调用所属的模型响应已过期（用户改变了方向），"
                            "已拒绝执行，未产生副作用。"
                        ),
                    },
                )
        except Exception:
            return

    @staticmethod
    async def _emit_stale_result(
        toolbox: Any,
        tool_name: str,
        pinned: int,
        current: int,
        operation: ToolOperationRow | None,
    ) -> None:
        """已派发操作的迟到结果：保留执行事实，但不把旧结果送入当前图决策。"""
        async with toolbox.db.transaction() as db:
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.ERROR,
                payload={
                    "scope": "stale_tool_result",
                    "tool": tool_name,
                    "pinned_control_version": pinned,
                    "current_control_version": current,
                    "operation_id": str(operation.operation_id) if operation else None,
                    "job_id": str(operation.job_id) if operation and operation.job_id else None,
                    "message": "旧工具结果未注入当前决策；远端副作用需单独核对。",
                },
            )


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
