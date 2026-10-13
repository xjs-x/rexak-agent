"""不完整模型响应的有限恢复（官方 middleware 接缝）。

设计要点（§3.2、§3.4）：
- 使用官方 `awrap_model_call`：wrapper **可以多次调用 handler**，因此恢复不需要在
  Runner 里写第二套"模型—工具"循环；
- 包装顺序必须是 `ResponseRecovery -> ContextBuilder -> ModelObserver -> 官方模型调用`，
  这样每次恢复尝试都会重新构造发送视图、独立入账；
- 压缩的 `before_model` hook 仍由官方图执行；恢复**只重新构造发送视图**，
  不手动再次调用整个图，也不删除 checkpoint 消息；
- 未被接受的响应在进入官方工具节点**之前**就被截住，绝不写成一条等待 ToolMessage
  的可执行历史；
- 默认「初次请求 + 最多两次自动恢复」。这是单次逻辑模型决策的保护值，
  **不是**任务尝试总数限制。
"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from sqlalchemy import func, select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.events import EventType
from agent_workbench.models.response_quality import (
    ResponseQuality,
    batch_rejection,
    classify_response,
    recovery_feedback,
)
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.persistence.tables import ModelCallRow

log = get_logger("agent_workbench.response_recovery")


class ModelResponseUnavailable(RuntimeError):
    """恢复耗尽：没有得到可接受的模型响应。

    由 Runner 映射为既有 `failed`，`stop_reason` 明确为"模型响应不可用"；
    checkpoint 与领域成果**保留**，用户继续或提交新指令后可建立新恢复周期。
    """


class ModelResponseRefused(RuntimeError):
    """供应商明确拒绝。**不自动重试以绕过拒绝**。"""


@dataclass(slots=True)
class RecoveryScope:
    """请求级恢复上下文。

    用 ContextVar 传递而不是 middleware 实例字段：middleware 实例在多次运行间共享，
    可变字段会串台（§3.4）。
    """

    logical_call_id: UUID
    attempt_index: int = 0
    max_recoveries: int = 2
    reason: str | None = None
    last_quality: ResponseQuality | None = None


#: 只在单次请求期间有效；`finally` 中必须 reset。
recovery_scope: ContextVar[RecoveryScope | None] = ContextVar("model_recovery_scope", default=None)


def logical_decision_id(request: object, context: RunContext) -> UUID:
    """锚定一次待执行的模型决策。

    至少包含 run、片段与**稳定消息 ID 范围**；不能只靠消息数量区分
    （恢复尝试会追加反馈，条数会变，但锚点来自原始请求，保持稳定）。
    """
    messages = list(getattr(request, "messages", None) or [])
    ids = [str(getattr(message, "id", "") or "") for message in messages]
    anchor = f"{ids[0] if ids else '-'}..{ids[-1] if ids else '-'}:{len(messages)}"
    system_text = str(getattr(getattr(request, "system_message", None), "content", "") or "")
    digest = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]
    segment = int((getattr(request, "state", None) or {}).get("segment_index") or 0)
    raw = f"{context.toolbox.run_id}|{segment}|{anchor}|{digest}"
    return UUID(bytes=hashlib.sha256(raw.encode("utf-8")).digest()[:16])


async def _recoveries_used(context: RunContext, logical_id: UUID) -> int:
    """读取同一逻辑调用的**已完成尝试数**，用于自动重启后不重新获得无限恢复额度。

    查询失败时按 0 处理（不阻断模型调用），但这只影响上限，不影响正确性。
    """
    try:
        async with context.toolbox.db.session() as db:
            count = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(ModelCallRow)
                        .where(
                            ModelCallRow.logical_call_id == logical_id,
                            ModelCallRow.run_id == context.toolbox.run_id,
                        )
                    )
                ).scalar_one()
            )
    except Exception as exc:  # 观测降级，不改变模型调用的正确性
        log.warning("recovery_quota_read_failed", error_type=type(exc).__name__)
        return 0
    return max(0, count - 1)


async def _record_quality(
    context: RunContext, logical_id: UUID, attempt: int, quality: ResponseQuality
) -> None:
    """把响应质量写进事件流，便于复盘"当时到底收到了什么"。"""
    try:
        async with context.toolbox.db.transaction() as db:
            await append_event(
                db,
                session_id=context.toolbox.session_id,
                run_id=context.toolbox.run_id,
                event_type=EventType.MODEL_RESPONSE_QUALITY,
                payload={
                    "logical_call_id": str(logical_id),
                    "attempt_index": attempt,
                    **quality.to_payload(),
                    "note": (
                        "网络成功与语义可用是两个维度；本事件只记录可观察的分类结果，"
                        "不包含供应商推理正文。"
                    ),
                },
            )
    except Exception as exc:
        log.warning("response_quality_event_failed", error_type=type(exc).__name__)


class ResponseRecoveryMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    """把不完整响应拦在工具节点之前，并做有限恢复。"""

    def __init__(self, *, max_recoveries: int = 2, recovery_output_limit: int | None = None) -> None:
        super().__init__()
        self._max_recoveries = max(0, int(max_recoveries))
        self._recovery_output_limit = recovery_output_limit

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        context: RunContext | None = getattr(request.runtime, "context", None)
        if context is None or recovery_scope.get() is not None:
            # 没有运行上下文（诊断/装配）或已被外层恢复接管：不嵌套恢复。
            return await handler(request)

        logical_id = logical_decision_id(request, context)
        used = await _recoveries_used(context, logical_id)
        budget = max(0, self._max_recoveries - used)
        scope = RecoveryScope(
            logical_call_id=logical_id, max_recoveries=budget, attempt_index=0
        )
        token = recovery_scope.set(scope)
        feedback: str | None = None
        history: list[str] = []
        try:
            for attempt in range(budget + 1):
                scope.attempt_index = attempt
                scope.reason = feedback
                check = getattr(context.toolbox, "assert_control_current", None)
                if check is not None:
                    # 恢复每次检查控制版本、暂停、取消、租约；已失效的响应不再被接受。
                    await check()
                current = (
                    request
                    if feedback is None
                    else _with_recovery_feedback(request, feedback, self._recovery_output_limit)
                )
                response = await handler(current)
                quality = classify_response(response)
                rejection: str | None = None
                if quality.accepted:
                    # 整批可接受性检查：不完整 JSON、缺少稳定 id 或同批多次 update_plan
                    # 都不执行该批，而是返回简短格式反馈后恢复。
                    rejection = batch_rejection(response)
                    if rejection is not None:
                        quality = ResponseQuality(
                            outcome="invalid_tool_call",
                            finish_reason=quality.finish_reason,
                            visible_chars=quality.visible_chars,
                            valid_tool_count=quality.valid_tool_count,
                            invalid_tool_count=quality.invalid_tool_count + 1,
                        )
                scope.last_quality = quality
                await _record_quality(context, logical_id, attempt, quality)

                if quality.accepted:
                    return response
                if quality.outcome == "refused":
                    raise ModelResponseRefused(
                        "供应商明确拒绝了本次请求；不会自动重试以绕过拒绝。"
                        "已保留可见原因与当前进度，请检查目标内容或调整请求。"
                    )
                feedback = rejection if rejection is not None else recovery_feedback(quality)

                history.append(quality.outcome)
                if self._should_stop_early(history):
                    break

            raise ModelResponseUnavailable(
                f"连续 {len(history)} 次都没有得到可接受的模型响应"
                f"（最后分类：{history[-1] if history else 'unknown'}）。"
                "已保留 checkpoint 与已完成的领域成果；本次用量已记录。"
                "这属于模型响应不可用，**不是**目标网站的阴性安全结论。"
                "用户继续或提交新指令后可建立新的恢复周期。"
            )
        finally:
            recovery_scope.reset(token)

    def _should_stop_early(self, history: list[str]) -> bool:
        """配置无变化的连续截断：提前结束同一恢复周期，避免机械重发相同请求。"""
        if self._recovery_output_limit is not None:
            return False
        return len(history) >= 2 and history[-1] == history[-2] == "truncated"


def _with_recovery_feedback(request, feedback: str, output_limit: int | None):  # type: ignore[no-untyped-def]
    """为恢复尝试重新构造发送视图：追加系统反馈 + 可选的恢复输出额度。

    不删除 checkpoint 消息，也不把两次输出拼成一次回答。
    """
    messages = [*request.messages, SystemMessage(content=feedback)]
    overrides: dict[str, object] = {"messages": messages}
    if output_limit is not None:
        settings = dict(request.model_settings or {})
        settings["max_tokens"] = int(output_limit)
        overrides["model_settings"] = settings
    return request.override(**overrides)  # type: ignore[arg-type]


__all__ = [
    "ModelResponseRefused",
    "ModelResponseUnavailable",
    "RecoveryScope",
    "ResponseRecoveryMiddleware",
    "logical_decision_id",
    "recovery_scope",
]
