"""模型调用观测（实际模型请求前后）。

职责（§6.2、§11.2）：
- 为**一次真实网络尝试**生成 `model_call_id`，记录 `logical_call_id` 与 `retry_index`；
- 记录耗时、用量、价格快照；
- 按 `model_call_id` **幂等更新结算**（流式累计值 + 最终总量不能重复计价）；
- 用量持久化失败时留下待同步标记并幂等重放，**不因费用组件故障重复发出模型请求**。

首个可见输出只在图流分片 ID 与最终 AIMessage ID 精确匹配时记录；
无法匹配时保持未知，不能把任意 SSE 到达时间冒充供应商首 token。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import PriceCatalog, default_price_catalog
from agent_workbench.contracts.enums import Phase
from agent_workbench.contracts.events import EventType
from agent_workbench.observability.costs import compute_cost, normalize_usage
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import ArtifactRepository, ModelCallRepository


def logical_call_id_for(
    *,
    run_id: UUID,
    segment_index: int,
    message_count: int,
    system_prompt_digest: str,
) -> UUID:
    """**确定性**逻辑调用 ID。

    图重放时消息数与系统提示一致 -> 得到同一个 logical_call_id，
    因此重放不会产生"新的逻辑调用"，只会新增物理尝试（§11.2）。
    """
    raw = f"{run_id}|{segment_index}|{message_count}|{system_prompt_digest}".encode()
    return UUID(bytes=hashlib.sha256(raw).digest()[:16])


class ModelRequestTimeout(TimeoutError):
    """单次模型请求超过墙钟上限；用量可能不完整。"""


class ModelObserverMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(
        self,
        price_catalog: PriceCatalog | None = None,
        *,
        request_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__()
        self._catalog = price_catalog or default_price_catalog()
        self._request_timeout_seconds = request_timeout_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._retry_tracker: dict[str, int] = {}

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        runtime: Any = request.runtime
        context: RunContext | None = getattr(runtime, "context", None)
        state = request.state or {}

        if context is None:
            # 没有运行上下文时不写台账（例如纯框架自测），但仍继续执行请求
            return await handler(request)

        toolbox = context.toolbox
        run_id = toolbox.run_id
        session_id = toolbox.session_id
        segment_index = int(state.get("segment_index") or 0)
        prompt_digest = hashlib.sha256(
            str(getattr(request.system_message, "content", "") or "").encode("utf-8")
        ).hexdigest()[:16]

        logical_id = logical_call_id_for(
            run_id=run_id,
            segment_index=segment_index,
            message_count=len(request.messages),
            system_prompt_digest=prompt_digest,
        )
        tracker_key = str(logical_id)
        retry_index = self._retry_tracker.get(tracker_key, 0)
        self._retry_tracker[tracker_key] = retry_index + 1

        model_call_id = uuid4()
        submitted_at = datetime.now(UTC)

        # 先落一行 pending，保证"发出请求"这件事被记录；失败也不能阻止模型调用
        try:
            async with toolbox.db.transaction() as db:
                await ModelCallRepository(db).begin_attempt(
                    logical_call_id=logical_id,
                    model_call_id=model_call_id,
                    run_id=run_id,
                    session_id=session_id,
                    phase=str(Phase.MODEL_COMPLETE),
                    retry_index=retry_index,
                    provider_id=toolbox.provider_id,
                    model_name=toolbox.model_name,
                    price_snapshot={
                        "catalog_version": self._catalog.version,
                        "provider_id": toolbox.provider_id,
                        "model_name": toolbox.model_name,
                    },
                    attempts_observable=toolbox.sdk_retries_disabled,
                )
                await append_event(
                    db,
                    session_id=session_id,
                    run_id=run_id,
                    event_type=EventType.RUN_STATUS,
                    payload={
                        "scope": "model_request",
                        "phase": "waiting",
                        "model_call_id": str(model_call_id),
                    },
                )
        except Exception as exc:
            import structlog

            structlog.get_logger("agent_workbench.model_observer").warning(
                "model_call_begin_failed",
                model_call_id=str(model_call_id),
                reason=f"{type(exc).__name__}: {exc}",
            )

        error_code: str | None = None
        response: Any = None
        total_timeout = asyncio.timeout(self._request_timeout_seconds)
        idle_timeout = asyncio.timeout(self._idle_timeout_seconds)

        def note_activity() -> None:
            if self._idle_timeout_seconds is not None and not idle_timeout.expired():
                idle_timeout.reschedule(
                    asyncio.get_running_loop().time() + self._idle_timeout_seconds
                )

        try:
            try:
                async with total_timeout, idle_timeout:
                    with context.model_stream.track_call(str(model_call_id), note_activity):
                        response = await handler(request)
            except TimeoutError as exc:
                if idle_timeout.expired():
                    boundary = f"模型连续 {self._idle_timeout_seconds} 秒没有返回有效分片"
                elif total_timeout.expired():
                    boundary = f"单次模型请求超过 {self._request_timeout_seconds} 秒保护上限"
                else:
                    boundary = "模型流读取超时"
                raise ModelRequestTimeout(
                    f"{boundary}；本次用量可能不完整。已停止本地等待，"
                    "未自动重发；点击继续可从已保存状态恢复"
                ) from exc
            return response
        except asyncio.CancelledError:
            error_code = "CancelledError"
            raise
        except Exception as exc:
            error_code = type(exc).__name__
            raise
        finally:
            completed_at = datetime.now(UTC)
            messages = getattr(response, "result", None) if response is not None else None
            first_token_at = context.model_stream.take_for_response(
                messages, submitted_at=submitted_at, completed_at=completed_at
            )
            usage_raw = _extract_usage(messages)
            totals = normalize_usage(usage_raw)
            price = self._catalog.find(toolbox.provider_id, toolbox.model_name)
            cost = compute_cost(totals, price, self._catalog)
            try:
                async with toolbox.db.transaction() as db:
                    await ModelCallRepository(db).reconcile(
                        model_call_id=model_call_id,
                        usage_status=str(totals.usage_status),
                        cost_status=str(cost.status),
                        input_tokens=totals.input_tokens,
                        output_tokens=totals.output_tokens,
                        cache_read_tokens=totals.cache_read_tokens,
                        cache_write_tokens=totals.cache_write_tokens,
                        reasoning_tokens=totals.reasoning_tokens,
                        raw_usage=totals.raw_usage,
                        cost_amount=cost.amount,
                        cost_currency=cost.currency,
                        first_token_at=first_token_at,
                        completed_at=completed_at,
                        error_code=error_code,
                    )
                    await append_event(
                        db,
                        session_id=session_id,
                        run_id=run_id,
                        event_type=EventType.RUN_STATUS,
                        payload={
                            "scope": "model_request",
                            "phase": "finished",
                            "model_call_id": str(model_call_id),
                            "error_code": error_code,
                        },
                    )
                    await append_event(
                        db,
                        session_id=session_id,
                        run_id=run_id,
                        event_type=EventType.USAGE_UPDATED,
                        payload={
                            "model_call_id": str(model_call_id),
                            "logical_call_id": str(logical_id),
                            "retry_index": retry_index,
                            "usage_status": str(totals.usage_status),
                            "cost_status": str(cost.status),
                            "input_tokens": totals.input_tokens,
                            "output_tokens": totals.output_tokens,
                            "cost_amount": str(cost.amount) if cost.amount is not None else None,
                            "cost_currency": cost.currency,
                            "duration_ms": (completed_at - submitted_at).total_seconds() * 1000,
                            "first_visible_output_ms": (
                                (first_token_at - submitted_at).total_seconds() * 1000
                                if first_token_at
                                else None
                            ),
                            "note": ("应用侧估算，不等于供应商账单；未知用量不计为 0。"),
                        },
                    )
            except Exception as exc:
                await _mark_pending_sync(toolbox, model_call_id, exc)
            if response is not None:
                await _archive_response(toolbox, model_call_id, messages)


def _response_text(messages: Any) -> str:
    """只归档模型文本块；工具参数和非文本多模态块不作为对话正文。"""
    if not messages:
        return ""
    pieces: list[str] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            pieces.extend(
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
    return "\n".join(piece for piece in pieces if piece)


async def _archive_response(toolbox: Any, model_call_id: UUID, messages: Any) -> None:
    """归档失败只影响历史浏览，绝不重复调用模型或覆盖用量结算。"""
    body = _response_text(messages)
    if not body:
        return
    try:
        from agent_workbench.contracts.enums import Sensitivity
        from agent_workbench.evidence.artifacts import ArtifactStore

        store = ArtifactStore(toolbox.db.settings)
        stored = await asyncio.to_thread(
            store.write_bytes,
            body.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            sensitivity=Sensitivity.CONFIDENTIAL,
        )
        async with toolbox.db.transaction() as db:
            call = await ModelCallRepository(db).get(model_call_id)
            if call is None or call.response_artifact_id is not None:
                return
            artifact = await ArtifactRepository(db).create(
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
                relative_key=stored.relative_key,
                session_id=call.session_id,
                source_operation_id=None,
                sensitivity="confidential",
                availability="available",
                captured_at=stored.captured_at,
                summary="模型回复正文归档",
            )
            call.response_artifact_id = artifact.id
    except Exception as exc:
        import structlog

        structlog.get_logger("agent_workbench.model_observer").warning(
            "model_response_archive_failed",
            model_call_id=str(model_call_id),
            reason=f"{type(exc).__name__}: {exc}",
        )


def _extract_usage(messages: Any) -> dict[str, Any]:
    if not messages:
        return {}
    for message in reversed(list(messages)):
        if isinstance(message, AIMessage):
            collected: dict[str, Any] = {}
            meta = getattr(message, "usage_metadata", None)
            if isinstance(meta, dict):
                collected.update(meta)
            response_metadata = getattr(message, "response_metadata", None)
            if isinstance(response_metadata, dict):
                for key in ("token_usage", "usage"):
                    value = response_metadata.get(key)
                    if isinstance(value, dict):
                        collected.update(value)
                collected.setdefault(
                    "_raw_response_metadata_keys", sorted(response_metadata.keys())
                )
            if collected:
                return collected
    return {}


async def _mark_pending_sync(toolbox: Any, model_call_id: UUID, exc: BaseException) -> None:
    """把用量标为**待同步**（由 CLI / 后台任务幂等重放）。

    这是"费用组件故障不重复发请求"的落地：只记录待同步，**不重试模型调用**（§11.3 规则 6）。
    """
    import structlog
    from sqlalchemy import update

    from agent_workbench.persistence.tables import ModelCallRow

    marked = False
    try:
        async with toolbox.db.transaction() as db:
            await db.execute(
                update(ModelCallRow)
                .where(ModelCallRow.model_call_id == model_call_id)
                .values(pending_sync=True)
            )
        marked = True
    except Exception:
        marked = False

    structlog.get_logger("agent_workbench.model_observer").warning(
        "model_call_usage_pending_sync",
        model_call_id=str(model_call_id),
        persisted_flag=marked,
        reason=f"{type(exc).__name__}: {exc}",
        note="用量未写入数据库；已尽力标记待同步，不会因此重复发出模型请求。",
    )
