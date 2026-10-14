"""发送视图的完整预算；估算不冒充供应商 tokenizer 或账单。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from agent_workbench.config.catalog import ProviderProfile
from agent_workbench.models.provider import request_payload


@dataclass(frozen=True, slots=True)
class RequestBudgetEstimate:
    estimated_input_tokens: int
    allowed_input_tokens: int
    components: dict[str, int]
    estimation_method: str = "utf8_bytes_div_3_plus_message_overhead_v1"
    uncertain: bool = True
    safety_margin_ratio: float = 0.1
    payload_adapter: str = "provider"
    #: 本次估算实际使用的输出额度（恢复尝试可能与之不同）。
    output_limit: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, estimate: RequestBudgetEstimate):
        self.estimate = estimate
        super().__init__(
            f"完整请求估计输入 {estimate.estimated_input_tokens} tokens，超过允许输入 "
            f"{estimate.allowed_input_tokens}；已阻止发送，保留 checkpoint 以便恢复。"
        )


def serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def estimate_value(value: Any) -> int:
    return math.ceil(len(serialized(value).encode("utf-8")) / 3)


def estimate_request(
    messages: Sequence[BaseMessage],
    system_message: SystemMessage | None,
    tool_schemas: Sequence[Any],
    provider_profile: ProviderProfile,
    *,
    model: Any = None,
    model_settings: dict[str, Any] | None = None,
    tool_choice: Any = None,
    safety_margin_ratio: float | None = None,
    output_limit: int | None = None,
) -> RequestBudgetEstimate:
    if safety_margin_ratio is None:
        safety_margin_ratio = provider_profile.budget_safety_margin_ratio
    if not 0 <= safety_margin_ratio < 1:
        raise ValueError("safety_margin_ratio 必须在 [0, 1) 内")
    selected = [system_message, *messages] if system_message is not None else messages
    payload = request_payload(
        model,
        selected,
        tools=tool_schemas,
        model_settings=model_settings,
        tool_choice=tool_choice,
        profile=provider_profile,
    )
    # 使用适配器的最终 wire 设置，覆盖恢复尝试及模型绑定的输出额度。
    effective_output = (
        int(output_limit)
        if output_limit is not None
        else int(
            payload.get("max_completion_tokens")
            or payload.get("max_output_tokens")
            or payload.get("max_tokens")
            or provider_profile.effective_output_limit
        )
    )
    if effective_output <= 0:
        raise ValueError("请求输出额度必须大于零")
    allowed = int(
        max(0, provider_profile.context_limit - effective_output) * (1 - safety_margin_ratio)
    )
    if provider_profile.max_input_tokens is not None:
        allowed = min(allowed, int(provider_profile.max_input_tokens))
    components = {
        "system": 0,
        "messages": 0,
        "tools": 0,
        "provider_return_fields": 0,
        "protocol": 0,
    }
    for index, wire in enumerate(payload.get("messages", [])):
        visible = dict(wire)
        # 字段按实际 wire 计量，模型层之外不判断供应商协议。
        returned = {
            key: visible.pop(key)
            for key in tuple(visible)
            if key.endswith("_content") and key != "content"
        }
        key = "system" if system_message is not None and index == 0 else "messages"
        components[key] += estimate_value(visible) + 4
        if returned:
            components["provider_return_fields"] += estimate_value(returned)
    components["tools"] = estimate_value(payload["tools"]) if "tools" in payload else 0
    components["protocol"] = (
        estimate_value(
            {key: value for key, value in payload.items() if key not in {"messages", "tools"}}
        )
        + 8
    )
    return RequestBudgetEstimate(
        estimated_input_tokens=sum(components.values()),
        allowed_input_tokens=allowed,
        components=components,
        safety_margin_ratio=safety_margin_ratio,
        payload_adapter=type(model).__name__ if model is not None else "protocol_preview",
        output_limit=effective_output,
    )


def ensure_fits(estimate: RequestBudgetEstimate) -> None:
    if estimate.estimated_input_tokens > estimate.allowed_input_tokens:
        raise ContextBudgetExceeded(estimate)


def message_inventory(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """日志只记录身份、长度与内容哈希，不复制供应商回传字段正文。"""
    result = []
    for message in messages:
        body = serialized(message.content)
        result.append(
            {
                "id": message.id,
                "role": message.type,
                "content_chars": len(body),
                "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "tool_call_id": getattr(message, "tool_call_id", None),
                "tool_call_ids": [call["id"] for call in getattr(message, "tool_calls", [])],
                "provider_return_fields": [
                    {"name": key, "chars": len(value)}
                    for key, value in message.additional_kwargs.items()
                    if key.endswith("_content") and isinstance(value, str)
                ],
                "source_artifact_id": message.additional_kwargs.get("source_artifact_id"),
            }
        )
    return result
