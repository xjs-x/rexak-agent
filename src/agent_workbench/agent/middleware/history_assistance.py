"""正式请求边界的历史辅助：最终 ContextBuilder 后、ModelObserver 前，默认不装配。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.decision_memory import build_blackboard
from agent_workbench.agent.history_assistance import all_tools_paired, memory_view
from agent_workbench.agent.signals import GraphRunAborted, LeaseLostError, StaleResponseError
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import ProviderProfile
from agent_workbench.domain.passive_reflection import encoded
from agent_workbench.models.context_budget import estimate_request, message_inventory
from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.tools.history_feedback import claim_reference, pending_reference

log = structlog.get_logger(__name__)
CONTROL_SIGNALS = (GraphRunAborted, LeaseLostError, StaleResponseError, asyncio.CancelledError)


def reference_messages(messages: list, bundle: dict) -> tuple[list, int]:
    """只改发送视图，不修改原 message 对象、checkpoint 或业务工具回执。"""
    view = list(messages)
    indices = [i for i, m in enumerate(view) if isinstance(m, ToolMessage)]
    if indices:
        index = indices[-1]
        original = view[index]
        try:
            content = json.loads(str(original.content))
        except ValueError:
            content = None
        if not isinstance(content, dict) or "_host_history_reference" in content:
            content = {"original_tool_result": original.content}
        content["_host_history_reference"] = bundle
        view[index] = original.model_copy(update={"content": encoded(content)})
    else:
        index = len(view)
        view.append(
            AIMessage(
                content=encoded({"_host_history_reference": bundle}),
                id="host-history-reference:" + hashlib.sha256(encoded(bundle).encode()).hexdigest(),
            )
        )
    return view, index


class HistoryAssistanceMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(self, profile: ProviderProfile | None = None) -> None:
        self.profile = profile

    async def awrap_model_call(self, request, handler):
        context = getattr(request.runtime, "context", None)
        box = getattr(context, "toolbox", None)
        if box is None or context is None:
            return await handler(request)
        settings = box.settings
        mode = settings.history_assistance_mode
        manifest = box.context_manifest if box.context_manifest is not None else {}
        manifest.pop("history_assistance", None)
        box.history_assistance_manifest = None
        if mode == "passive":
            box.history_assistance_visible_context = None
        if mode == "off" or graph_review_scope.get() or not all_tools_paired(request.messages):
            return await handler(request)
        info: dict[str, Any] = {
            "mode": mode,
            "memory": "not_available",
            "reflection": "not_available",
            "bytes": 0,
            "memory_bytes": 0,
            "reflection_bytes": 0,
            "selected_refs": [],
            "effective_reference_byte_limit": settings.history_assistance_input_bytes,
            "effective_record_limit": settings.history_assistance_memory_limit,
            "selection": {"status": "not_attempted", "candidate_count": None,
                          "selected_refs": [], "omitted": [], "projection_bytes": 0},
        }
        outgoing = request
        bundle: dict[str, Any] = {
            "origin": "controller_generated_reference_not_user_instruction",
            "authority": "untrusted_reference_data_only",
        }
        pending = None
        if mode == "passive":
            try:
                async with asyncio.timeout(settings.history_assistance_retrieval_seconds):
                    pending = await pending_reference(
                        box, context.thread_id, list(request.messages)
                    )
                if pending:
                    bundle["reflection"] = pending[0]
            except CONTROL_SIGNALS:
                raise
            except Exception as exc:
                info["reflection"] = "unavailable:" + type(exc).__name__
                log.warning("history_feedback_unavailable", error_type=type(exc).__name__)
        started = time.perf_counter()
        retrieved = False
        try:
            async with asyncio.timeout(settings.history_assistance_retrieval_seconds):
                board, _ = await build_blackboard(
                    box, dict(request.state or {}), list(request.messages)
                )
            retrieved = True
            info["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 3)
            byte_limit = settings.history_assistance_input_bytes - len(encoded(bundle).encode()) - 64
            if self.profile is not None:
                base_budget = estimate_request(
                    request.messages, request.system_message, request.tools, self.profile,
                    model=request.model, model_settings=request.model_settings,
                    tool_choice=request.tool_choice,
                )
                # UTF-8 字节预算为预筛选；最终仍按真实适配器完整 payload 复核。
                byte_limit = min(byte_limit, max(0,
                    (base_budget.allowed_input_tokens - base_budget.estimated_input_tokens) * 2 - 512))
            selection: dict[str, Any] = {}
            view = memory_view(
                board,
                str(box.run_id),
                settings.history_assistance_memory_limit,
                byte_limit,
                set(bundle.get("reflection", {}).get("historical_operations", [])),
                messages=list(request.messages), trace=selection,
            )
            info["selection"] = selection
            if byte_limit <= 0 and selection.get("status") == "omitted_by_budget":
                info["projection"] = "omitted:insufficient_context_budget"
            if view:
                bundle["memory"] = view
                info["memory"] = "selected"
            else:
                info["memory"] = selection.get("status", "no_relevant_history")
        except CONTROL_SIGNALS:
            raise
        except Exception as exc:
            if retrieved:
                info["memory"] = "projection_error"
                info["projection_error_type"] = type(exc).__name__
                log.warning("history_assistance_omitted", error_type=type(exc).__name__)
            else:
                info["memory"] = "retrieval_timeout" if isinstance(exc, TimeoutError) else "retrieval_error"
                info["retrieval_error_type"] = type(exc).__name__
                info["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 3)
                log.warning("history_memory_unavailable", error_type=type(exc).__name__)
        # 回退只作用于可选投影；模型 handler 不在 try 内，不会因为解析/供应商错误重调。
        if "memory" in bundle or "reflection" in bundle:
            try:
                if len(encoded(bundle).encode("utf-8")) > settings.history_assistance_input_bytes:
                    bundle.pop("memory", None)
                    info["memory"] = "omitted_by_budget"
                if len(encoded(bundle).encode("utf-8")) > settings.history_assistance_input_bytes:
                    raise ValueError("optional_history_input_limit")
                if "memory" not in bundle and "reflection" not in bundle:
                    raise ValueError("optional_history_empty")
                messages, index = reference_messages(list(request.messages), bundle)
                budget = None
                if self.profile is not None:
                    budget = estimate_request(
                        messages,
                        request.system_message,
                        request.tools,
                        self.profile,
                        model=request.model,
                        model_settings=request.model_settings,
                        tool_choice=request.tool_choice,
                    )
                    while budget.estimated_input_tokens > budget.allowed_input_tokens:
                        memory = bundle.get("memory")
                        if not memory or not memory["execution_evidence"]:
                            raise ValueError("optional_history_exceeds_request_budget")
                        removed = memory["execution_evidence"].pop()
                        info.setdefault("context_budget_omitted_refs", []).append(removed["record_ref"])
                        memory["model_claim_references"] = []
                        if not memory["execution_evidence"]:
                            bundle.pop("memory")
                            info["memory"] = "omitted_by_budget"
                        if "memory" not in bundle and "reflection" not in bundle:
                            raise ValueError("optional_history_exceeds_request_budget")
                        messages, index = reference_messages(list(request.messages), bundle)
                        budget = estimate_request(
                            messages, request.system_message, request.tools, self.profile,
                            model=request.model, model_settings=request.model_settings,
                            tool_choice=request.tool_choice,
                        )
                await box.assert_control_current()
                if pending and not claim_reference(box, context.thread_id, pending[0], pending[1]):
                    raise ValueError("history_feedback_already_claimed")
                outgoing = request.override(messages=messages)
                info.update(
                    message_index=index,
                    role=messages[index].type,
                    bytes=len(encoded(bundle).encode("utf-8")),
                    memory_bytes=len(encoded(bundle["memory"]).encode("utf-8")) if "memory" in bundle else 0,
                    reflection_bytes=len(encoded(bundle["reflection"]).encode("utf-8")) if "reflection" in bundle else 0,
                    reference_sha256=hashlib.sha256(encoded(bundle).encode()).hexdigest(),
                    reflection="claimed_for_normal_request" if pending else info["reflection"],
                    delivery="request_view_only_requires_model_observation",
                    original_tool_messages_unchanged=True,
                    selected_refs=[v["record_ref"] for v in
                        bundle.get("memory", {}).get("execution_evidence", [])],
                    material_hashes=[v["material_sha256"] for v in
                        bundle.get("memory", {}).get("execution_evidence", [])],
                )
                manifest["selected_messages"] = message_inventory(messages)
                if budget is not None:
                    manifest["budget"] = budget.to_dict()
                    manifest.update(
                        estimated_tokens=budget.components,
                        total_estimated_tokens=budget.estimated_input_tokens,
                        token_estimation_method=budget.estimation_method,
                    )
            except CONTROL_SIGNALS:
                raise
            except Exception as exc:
                info["projection"] = "omitted:" + type(exc).__name__
                info["bytes"] = 0
                info["memory_bytes"] = 0
                info["reflection_bytes"] = 0
                info["selected_refs"] = []
                if info["memory"] == "selected":
                    info["memory"] = "projection_error"
                log.warning("history_assistance_omitted", error_type=type(exc).__name__)
        if "selection" in info:
            info["selection"]["selected_refs"] = info["selected_refs"]
            info["selection"]["projection_bytes"] = info["memory_bytes"]
            if info["memory"] in {"omitted_by_budget", "projection_error", "retrieval_timeout", "retrieval_error"}:
                info["selection"]["status"] = info["memory"]
        box.history_assistance_manifest = info
        manifest["history_assistance"] = info
        box.context_manifest = manifest
        result = await handler(outgoing)
        if mode == "passive":
            from agent_workbench.tools.history_action_shadow import visible_context

            try:
                box.history_assistance_visible_context = visible_context(
                    list(outgoing.messages),
                    context.thread_id,
                    settings.history_action_shadow_input_bytes,
                )
            except Exception as exc:
                # 没有可见性来源时只弃判观察；不改变已经成功返回的模型结果。
                log.warning("history_decision_view_unavailable", error_type=type(exc).__name__)
        return result
