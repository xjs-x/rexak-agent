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
import json
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage
from pydantic import ValidationError
from sqlalchemy import func, select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.events import EventType
from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.models.response_quality import (
    ResponseQuality,
    batch_rejection,
    classify_response,
    last_ai_message,
    recovery_feedback,
)
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.persistence.tables import ModelCallRow

log = get_logger("agent_workbench.response_recovery")


def _review_response(response, tools):
    """分类适配器解析后的调用；不检查正文含义或输出工具参数。"""
    message = last_ai_message(response)
    quality = classify_response(response)
    calls = [*message.tool_calls, *message.invalid_tool_calls] if message else []
    details = {
        "response_id": message.id if message else None,
        "finish_reason": quality.finish_reason,
        "tool_names": [str(c.get("name") or "")[:80] for c in calls][:8],
        "tool_call_ids": [str(c.get("id") or "")[:128] for c in calls][:8],
    }
    if quality.outcome == "refused":
        return "provider_error", {**details, "error_category": "refused"}
    if quality.outcome == "truncated":
        return "invalid_arguments", {**details, "error_category": "truncated"}
    if message and message.invalid_tool_calls:
        return "invalid_arguments", {**details, "error_category": "invalid_tool_call"}
    if not message or not message.tool_calls:
        return "no_tool_call", details
    names = {c["name"] for c in message.tool_calls}
    if names - {"get_graph", "update_graph"}:
        return "disallowed_tool", details
    if sum(c["name"] == "update_graph" for c in message.tool_calls) > 1:
        return "invalid_arguments", {**details, "error_category": "multiple_updates"}
    schemas = {t.name: t.tool_call_schema for t in tools if not isinstance(t, dict)}
    for call in message.tool_calls:
        if not isinstance(call["args"], dict):
            return "invalid_arguments", {**details, "error_category": "non_object_arguments"}
        if call["name"] == "update_graph" and not call["args"].get("page_id"):
            return "invalid_arguments", {**details, "error_category": "missing_page_id"}
        try:
            if call["name"] in schemas:
                schemas[call["name"]].model_validate(call["args"])
        except ValidationError:
            return "invalid_arguments", {**details, "error_category": "schema_validation"}
    return "submitted_update" if "update_graph" in names else "read_graph", details


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


async def _attempts_used(context: RunContext, logical_id: UUID) -> int:
    """已登记尝试（包括结果未知的 pending）均消耗额度；读失败不发送请求。"""
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
                            ModelCallRow.phase == "agent",
                        )
                    )
                ).scalar_one()
            )
    except Exception as exc:
        log.warning("recovery_quota_read_failed", error_type=type(exc).__name__)
        raise
    return count


async def _record_quality(
    context: RunContext, logical_id: UUID, attempt: int, quality: ResponseQuality, response: object
) -> None:
    """把响应质量写进事件流，便于复盘"当时到底收到了什么"。"""
    try:
        message = last_ai_message(response)
        async with context.toolbox.db.transaction() as db:
            await append_event(
                db,
                session_id=context.toolbox.session_id,
                run_id=context.toolbox.run_id,
                event_type=EventType.MODEL_RESPONSE_QUALITY,
                payload={
                    "logical_call_id": str(logical_id),
                    "attempt_index": attempt,
                    "model_call_id": message.response_metadata.get("workbench_model_call_id")
                    if message
                    else None,
                    "response_id": message.id if message else None,
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

    def __init__(
        self, *, max_recoveries: int = 2, recovery_output_limit: int | None = None
    ) -> None:
        super().__init__()
        self._max_recoveries = max(0, int(max_recoveries))
        self._recovery_output_limit = recovery_output_limit

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        context: RunContext | None = getattr(request.runtime, "context", None)
        if context is None or recovery_scope.get() is not None:
            # 没有运行上下文（诊断/装配）或已被外层恢复接管：不嵌套恢复。
            return await handler(request)

        from agent_workbench.domain.graph_sync import (
            GraphReviewExhausted,
            finish_review,
            record_review_outcome,
            review_ready,
        )

        box = context.toolbox
        automatic_review = getattr(getattr(box, "settings", None), "decision_memory_mode", "current") == "current"
        previous = next((m for m in reversed(request.messages) if isinstance(m, AIMessage)), None)
        previous_window = (getattr(previous, "additional_kwargs", {}) or {}).get(
            "workbench_graph_review_window"
        )
        failures = []
        if previous_window and previous is not None:
            call_ids = {c["id"] for c in previous.tool_calls if c["name"] == "update_graph"}
            for message in request.messages:
                if getattr(message, "tool_call_id", None) in call_ids:
                    try:
                        result = json.loads(str(message.content))
                    except ValueError:
                        continue
                    error = result.get("error") if isinstance(result, dict) else None
                    failures.append(
                        isinstance(error, dict) and error.get("code") == "validation_failed"
                    )
        if automatic_review and any(failures):
            async with box.db.transaction() as db:
                await record_review_outcome(
                    db, box, "validation_failed", error_category="tool_validation"
                )
        if automatic_review and await review_ready(box, previous_window, any(failures)):
            window_id = box.graph_review_window_id
            token = graph_review_scope.set(window_id)
            try:
                graph_tools = [
                    t
                    for t in request.tools
                    if (
                        t.get("name", (t.get("function") or {}).get("name"))
                        if isinstance(t, dict)
                        else t.name
                    )
                    in {"get_graph", "update_graph"}
                ]
                if not graph_tools:
                    raise GraphReviewExhausted("没有可用图工具")
                current = request.override(tools=graph_tools, tool_choice=None)
                # Only response format is repaired here; all tools still run in official Tools.
                # reserve_review guards each physical handler request, including restored attempts.
                for _ in range(2):
                    try:
                        response = await handler(current)
                    except GraphReviewExhausted:
                        raise
                    except Exception as exc:
                        from agent_workbench.agent.signals import (
                            GraphRunAborted,
                            LeaseLostError,
                            StaleResponseError,
                        )

                        if isinstance(exc, GraphRunAborted | LeaseLostError | StaleResponseError):
                            raise
                        async with box.db.transaction() as db:
                            await record_review_outcome(
                                db, box, "provider_error", error_category=type(exc).__name__
                            )
                            await finish_review(db, box, "provider_error")
                        raise
                    outcome, details = _review_response(response, graph_tools)
                    async with box.db.transaction() as db:
                        used = await record_review_outcome(db, box, outcome, **details)
                    if outcome in {"submitted_update", "read_graph"}:
                        message = last_ai_message(response)
                        assert message is not None
                        message.additional_kwargs["workbench_graph_review_window"] = window_id
                        return response
                    if outcome == "provider_error" or used >= 2:
                        async with box.db.transaction() as db:
                            await finish_review(
                                db, box, outcome if outcome == "provider_error" else "exhausted"
                            )
                        break
                    system = str(getattr(request.system_message, "content", "") or "")
                    current = current.override(
                        system_message=SystemMessage(
                            content=system
                            + f"\n【宿主：仅剩一次图核对格式修正】上次分类={outcome}。"
                            "请实际调用update_graph，并携带当前page_id；无变化也提交operations=[]。"
                            "不用正文模拟工具调用，不使用外部测试工具；暂停取消优先，正常最终答复不受补图限制。"
                        )
                    )
                # Restore the normal main request; its no-tool answer retains official END.
            except GraphReviewExhausted:
                async with box.db.transaction() as db:
                    await finish_review(db, box, "request_budget_exhausted")
            finally:
                graph_review_scope.reset(token)

        logical_id = logical_decision_id(request, context)
        used = await _attempts_used(context, logical_id)
        remaining = max(0, 1 + self._max_recoveries - used)
        if not remaining:
            raise ModelResponseUnavailable(
                "本次逻辑决策的响应恢复额度已用尽；保留 checkpoint，等待明确继续指令。"
            )
        scope = RecoveryScope(
            logical_call_id=logical_id, max_recoveries=self._max_recoveries, attempt_index=used
        )
        token = recovery_scope.set(scope)
        feedback: str | None = (
            "上一尝试未得到已接受的完整决策，请给出完整下一步或简短结论。" if used else None
        )
        history: list[str] = []
        try:
            for attempt in range(used, used + remaining):
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
                await _record_quality(context, logical_id, attempt, quality, response)

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
