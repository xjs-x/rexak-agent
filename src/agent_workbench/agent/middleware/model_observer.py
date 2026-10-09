"""模型调用观测（实际模型请求前后）。

职责（§6.2、§11.2）：
- 为**一次真实网络尝试**生成 `model_call_id`，记录 `logical_call_id` 与 `retry_index`；
- 记录耗时、用量、价格快照；
- 按 `model_call_id` **幂等更新结算**（流式累计值 + 最终总量不能重复计价）；
- 用量持久化失败时留下待同步标记并幂等重放，**不因费用组件故障重复发出模型请求**。

首 token 耗时由 Runner 的流式循环记录（那里才真正看得见增量块），
本中间件记录提交时间与完整响应时间；两者一起构成"模型等待"的分段。
"""

from __future__ import annotations

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
from agent_workbench.persistence.repositories import ModelCallRepository


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


class ModelObserverMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(self, price_catalog: PriceCatalog | None = None) -> None:
        super().__init__()
        self._catalog = price_catalog or default_price_catalog()
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
        except Exception:
            pass

        error_code: str | None = None
        response: Any = None
        try:
            response = await handler(request)
            return response
        except Exception as exc:
            error_code = type(exc).__name__
            raise
        finally:
            completed_at = datetime.now(UTC)
            messages = getattr(response, "result", None) if response is not None else None
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
                        completed_at=completed_at,
                        error_code=error_code,
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
                            "note": ("应用侧估算，不等于供应商账单；未知用量不计为 0。"),
                        },
                    )
            except Exception as exc:
                await _mark_pending_sync(toolbox, model_call_id, exc)


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
