"""所有用途的单次物理模型调用：身份、超时、取消、用量与请求审计。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import func, select

from agent_workbench.config.catalog import PriceCatalog, ProviderProfile, default_price_catalog
from agent_workbench.contracts.events import EventType
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.models.context_budget import ensure_fits, estimate_request
from agent_workbench.observability.costs import compute_cost, normalize_usage
from agent_workbench.observability.events import append_event
from agent_workbench.observability.model_stream import ModelStreamTracker
from agent_workbench.persistence.repositories import ArtifactRepository, ModelCallRepository
from agent_workbench.persistence.tables import ModelCallRow


class ModelRequestTimeout(TimeoutError):
    """单次模型请求超时，远端用量可能未知。"""


@dataclass(slots=True)
class InvocationScope:
    toolbox: Any
    model_stream: ModelStreamTracker = field(default_factory=ModelStreamTracker)


def response_messages(response: Any) -> Any:
    if isinstance(response, AIMessage):
        return [response]
    return getattr(response, "result", None) if response is not None else None


async def invoke_attempt(
    call: Callable[[], Awaitable[Any]],
    *,
    scope: InvocationScope,
    logical_id: UUID,
    phase: str,
    catalog: PriceCatalog | None = None,
    request_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    manifest: dict[str, Any] | None = None,
    require_recorded: bool = False,
) -> Any:
    """唯一网络尝试所有者；不透明重试，已交付流片段不拼接新输出。"""
    toolbox = scope.toolbox
    control_check = getattr(toolbox, "assert_control_current", None)
    if control_check is not None:
        await control_check()
    run_id, session_id = toolbox.run_id, toolbox.session_id
    catalog = catalog or default_price_catalog()
    retry_index = 0
    model_call_id = uuid4()
    submitted_at = datetime.now(UTC)
    begin_recorded = False

    # 先落一行 pending，保证"发出请求"这件事被记录；失败也不能阻止模型调用
    try:
        async with toolbox.db.transaction() as db:
            retry_index = (
                int(
                    (
                        await db.execute(
                            select(func.coalesce(func.max(ModelCallRow.retry_index), -1)).where(
                                ModelCallRow.logical_call_id == logical_id,
                                ModelCallRow.run_id == run_id,
                                ModelCallRow.phase == phase,
                            )
                        )
                    ).scalar_one()
                )
                + 1
            )
            if manifest is not None:
                await persist_manifest(toolbox, db, model_call_id, logical_id, phase, manifest)
            await ModelCallRepository(db).begin_attempt(
                logical_call_id=logical_id,
                model_call_id=model_call_id,
                run_id=run_id,
                session_id=session_id,
                phase=phase,
                retry_index=retry_index,
                provider_id=toolbox.provider_id,
                model_name=toolbox.model_name,
                price_snapshot={
                    "catalog_version": catalog.version,
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
        begin_recorded = True
    except Exception as exc:
        import structlog

        structlog.get_logger("agent_workbench.model_observer").warning(
            "model_call_begin_failed",
            model_call_id=str(model_call_id),
            logical_call_id=str(logical_id),
            run_id=str(run_id),
            session_id=str(session_id),
            phase=phase,
            error_code=type(exc).__name__,
        )
        if require_recorded:
            # 有限恢复依赖持久台账；未登记就发送会在重启后重新获得额度。
            raise

    error_code: str | None = None
    response: Any = None
    total_timeout = asyncio.timeout(request_timeout_seconds)
    idle_timeout = asyncio.timeout(idle_timeout_seconds)

    def note_activity() -> None:
        if idle_timeout_seconds is not None and not idle_timeout.expired():
            idle_timeout.reschedule(asyncio.get_running_loop().time() + idle_timeout_seconds)

    try:
        try:
            async with total_timeout, idle_timeout:
                with scope.model_stream.track_call(str(model_call_id), note_activity):
                    response = await call()
                    for message in response_messages(response) or []:
                        if isinstance(message, AIMessage):
                            message.response_metadata["workbench_model_call_id"] = str(
                                model_call_id
                            )
                    if total_timeout.expired() or idle_timeout.expired():
                        raise TimeoutError("调用在取消后迟到")
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise asyncio.CancelledError
                    control_check = getattr(toolbox, "assert_control_current", None)
                    if control_check is not None:
                        await control_check()
        except TimeoutError as exc:
            if idle_timeout.expired():
                boundary = f"模型连续 {idle_timeout_seconds} 秒没有返回有效分片"
            elif total_timeout.expired():
                boundary = f"单次模型请求超过 {request_timeout_seconds} 秒保护上限"
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
        messages = response_messages(response)
        first_token_at = scope.model_stream.take_for_response(
            messages, submitted_at=submitted_at, completed_at=completed_at
        )
        usage_raw = _extract_usage(messages)
        totals = normalize_usage(usage_raw)
        price = catalog.find(toolbox.provider_id, toolbox.model_name)
        cost = compute_cost(totals, price, catalog)
        reconciled = False
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
                        "phase": phase,
                        "usage_status": str(totals.usage_status),
                        "cost_status": str(cost.status),
                        "input_tokens": totals.input_tokens,
                        "output_tokens": totals.output_tokens,
                        "estimated_input_tokens": (manifest or {})
                        .get("budget", {})
                        .get("estimated_input_tokens")
                        if (manifest or {}).get("budget")
                        else None,
                        "estimation_error_tokens": totals.input_tokens
                        - manifest["budget"]["estimated_input_tokens"]
                        if manifest and manifest.get("budget") and totals.input_tokens is not None
                        else None,
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
            reconciled = True
        except Exception as exc:
            await _mark_pending_sync(toolbox, model_call_id, exc)
        if not begin_recorded or not reconciled:
            from agent_workbench.models.pending_observations import write_pending_observation

            record = {
                "model_call_id": str(model_call_id),
                "logical_call_id": str(logical_id),
                "run_id": str(run_id),
                "session_id": str(session_id),
                "phase": phase,
                "retry_index": retry_index,
                "attempts_observable": bool(toolbox.sdk_retries_disabled and begin_recorded),
                "provider_id": toolbox.provider_id,
                "model_name": toolbox.model_name,
                "submitted_at": submitted_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "first_token_at": first_token_at.isoformat() if first_token_at else None,
                "error_code": error_code,
                "usage_status": str(totals.usage_status),
                "cost_status": str(cost.status),
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "cache_read_tokens": totals.cache_read_tokens,
                "cache_write_tokens": totals.cache_write_tokens,
                "reasoning_tokens": totals.reasoning_tokens,
                "raw_usage": totals.raw_usage,
                "cost_amount": str(cost.amount) if cost.amount is not None else None,
                "cost_currency": cost.currency,
                "manifest": manifest,
                "price_snapshot": {
                    "catalog_version": catalog.version,
                    "retry_index_status": "known" if begin_recorded else "unknown",
                },
            }
            try:
                await asyncio.to_thread(
                    write_pending_observation, toolbox.db.settings.data_root, record
                )
            except OSError as exc:
                import structlog

                structlog.get_logger(__name__).error(
                    "model_observation_spool_failed",
                    model_call_id=str(model_call_id),
                    phase=phase,
                    error_code=type(exc).__name__,
                    accounting_status="unknown_no_retry",
                )
        if response is not None and error_code is None:
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
            error_code=type(exc).__name__,
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
            result = await db.execute(
                update(ModelCallRow)
                .where(ModelCallRow.model_call_id == model_call_id)
                .values(pending_sync=True)
            )
        marked = int(getattr(result, "rowcount", 0) or 0) > 0
    except Exception:
        marked = False

    structlog.get_logger("agent_workbench.model_observer").warning(
        "model_call_usage_pending_sync",
        model_call_id=str(model_call_id),
        persisted_flag=marked,
        reason=f"{type(exc).__name__}: {exc}",
        note="用量未写入数据库；已尽力标记待同步，不会因此重复发出模型请求。",
    )


class InvocationActivity(AsyncCallbackHandler):
    def __init__(self, scope: InvocationScope) -> None:
        self.scope = scope

    async def on_llm_new_token(
        self, token: str | list[str | dict[str, Any]], *, chunk: Any = None, **kwargs: Any
    ) -> None:
        message = getattr(chunk, "message", None)
        if not isinstance(message, AIMessageChunk):
            message = AIMessageChunk(content=token, id=str(kwargs.get("run_id") or "auxiliary"))
        self.scope.model_stream.observe((message, {"langgraph_node": "model"}))


async def invoke_chat_model(
    model: Any,
    messages: list[Any],
    *,
    scope: InvocationScope,
    phase: str,
    logical_id: UUID,
    profile: ProviderProfile | None = None,
    manifest: dict[str, Any] | None = None,
    catalog: PriceCatalog | None = None,
    request_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
) -> Any:
    if profile is not None:
        budget = estimate_request(messages, None, [], profile, model=model)
        ensure_fits(budget)
        manifest = {**(manifest or {}), "budget": budget.to_dict()}

    async def call() -> Any:
        return await model.ainvoke(
            messages,
            config={
                "callbacks": [InvocationActivity(scope)],
                "metadata": {
                    "workbench_phase": phase,
                    "workbench_run_id": str(scope.toolbox.run_id),
                    "lc_source": phase,
                },
            },
        )

    response = await invoke_attempt(
        call,
        scope=scope,
        logical_id=logical_id,
        phase=phase,
        manifest=manifest,
        catalog=catalog,
        request_timeout_seconds=request_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    from agent_workbench.models.response_quality import ModelResponseIncomplete, classify_response

    quality = classify_response(response)
    if not quality.accepted or quality.valid_tool_count:
        raise ModelResponseIncomplete(
            f"辅助模型响应不可用：phase={phase}，outcome={quality.outcome}"
        )
    return response


async def persist_manifest(
    toolbox: Any,
    db: Any,
    model_call_id: UUID,
    logical_id: UUID,
    phase: str,
    manifest: dict[str, Any],
    *,
    status: str = "prepared_before_network_attempt",
) -> None:
    from agent_workbench.maintenance.diagnostics import _safe

    record = _safe(
        {
            **manifest,
            "model_call_id": str(model_call_id),
            "logical_call_id": str(logical_id),
            "phase": phase,
        }
    )
    body = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    stored = await asyncio.to_thread(
        ArtifactStore(toolbox.db.settings).write_bytes, body, media_type="application/json"
    )
    artifact = await ArtifactRepository(db).create(
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        media_type=stored.media_type,
        relative_key=stored.relative_key,
        session_id=toolbox.session_id,
        captured_at=stored.captured_at,
        summary="最终请求 manifest（无供应商隐藏正文）",
        source_operation_id=None,
        sensitivity="internal",
        availability="available",
    )
    working = record.get("working_context") or {}
    recall = working.get("next") or {}
    if recall.get("review_due"):
        await append_event(
            db,
            session_id=toolbox.session_id,
            run_id=toolbox.run_id,
            event_type=EventType.LEADS_RECALLED,
            payload={
                "model_call_id": str(model_call_id),
                "manifest_artifact_id": str(artifact.id),
                "status": status,
                "selected_ids": recall.get("selected_ids") or [],
                "unfinished_count": recall.get("unfinished_count"),
                "review_age_rounds": recall.get("review_age_rounds"),
                "cursor_before": recall.get("cursor_before"),
                "cursor_after_selection": recall.get("cursor_after_selection"),
                "note": "请求准备时的展示候选，不证明已重新评估或网络成功。",
            },
        )
    await append_event(
        db,
        session_id=toolbox.session_id,
        run_id=toolbox.run_id,
        event_type=EventType.CONTEXT_SELECTED,
        payload={
            "model_call_id": str(model_call_id),
            "logical_call_id": str(logical_id),
            "phase": phase,
            "manifest_artifact_id": str(artifact.id),
            "estimated_input_tokens": (manifest.get("budget") or {}).get("estimated_input_tokens"),
            "allowed_input_tokens": (manifest.get("budget") or {}).get("allowed_input_tokens"),
            "status": status,
        },
    )
