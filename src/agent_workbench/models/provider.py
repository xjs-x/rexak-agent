"""供应商适配。

小米与百炼走 OpenAI 兼容协议，但**必须单独验证**：完整工具参数、分片工具调用、空响应、
超时、取消、流结束 usage、错误码与图片支持（§11.1）。

控制隐藏 SDK 重试：优先关闭 SDK 内置重试，由单一层做可观测重试；否则明确标记
"请求尝试/费用可能不完整"（§11.2）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _convert_message_to_dict
from pydantic import SecretStr, ValidationError

from agent_workbench.config.catalog import ProviderProfile, default_provider_profiles
from agent_workbench.config.secrets import SecretStore, build_secret_store
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.models.bailian import BailianChatOpenAI
from agent_workbench.models.xiaomi import XiaomiChatOpenAI


class ProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """一个 run 内固定的模型配置快照（§11.1：一个 run 固定模型配置快照）。"""

    profile: ProviderProfile
    temperature: float = 0.0
    max_retries: int = 0
    request_timeout_seconds: int = 120
    extra: dict[str, Any] | None = None

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "provider_id": self.profile.provider_id,
            "model_name": self.profile.model_name,
            "base_url": self.profile.base_url,
            "context_limit": self.profile.context_limit,
            "output_reserve": self.profile.output_reserve,
            "request_output_limit": self.profile.effective_output_limit,
            "recovery_output_limit": self.profile.recovery_output_limit,
            "max_response_recoveries": self.profile.max_response_recoveries,
            "input_budget_tokens": self.profile.input_budget_tokens,
            "max_input_tokens": self.profile.max_input_tokens,
            "budget_safety_margin_ratio": self.profile.budget_safety_margin_ratio,
            "profile_version": self.profile.profile_version,
            "token_estimation_method": self.profile.token_estimation_method,
            "supports_tools": self.profile.supports_tools,
            "supports_streaming": self.profile.supports_streaming,
            "supports_usage": self.profile.supports_usage,
            "supports_images": self.profile.supports_images,
            "temperature": self.temperature,
            "max_retries": self.max_retries,
            "secret_ref": self.profile.secret_ref,
            "provider_extra": self.profile.provider_extra,
            "request_timeout_seconds": self.request_timeout_seconds,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> ModelBinding:
        try:
            return cls(
                profile=ProviderProfile.model_validate(snapshot),
                temperature=snapshot.get("temperature", 0.0),
                max_retries=snapshot.get("max_retries", 0),
                request_timeout_seconds=snapshot["request_timeout_seconds"],
            )
        except (ValidationError, KeyError, TypeError) as exc:
            raise ProviderConfigurationError("任务的模型绑定快照无效") from exc


def resolve_provider_profile(
    provider_id: str | None = None,
    model_name: str | None = None,
    settings: Settings | None = None,
) -> ProviderProfile:
    """解析供应商档案。默认小米，启动器可选择百炼。

    base_url / model 允许被各供应商的环境变量覆盖，
    以便无代码改动切换端点。
    """
    cfg = settings or get_settings()
    pid = provider_id or cfg.default_provider
    profiles = default_provider_profiles()

    if pid == "xiaomi":
        candidate = model_name or cfg.xiaomi_model
        fallback = next(value for (key, _), value in profiles.items() if key == "xiaomi")
        profile = profiles.get(("xiaomi", candidate), fallback)
        return ProviderProfile(
            provider_id="xiaomi",
            model_name=candidate,
            base_url=cfg.xiaomi_base_url,
            secret_ref="env:XIAOMI_API_KEY",
            context_limit=profile.context_limit,
            output_reserve=cfg.output_reserve_tokens,
            request_output_limit=cfg.output_reserve_tokens,
            recovery_output_limit=cfg.recovery_output_limit_tokens,
            max_response_recoveries=cfg.max_response_recoveries,
            budget_safety_margin_ratio=profile.budget_safety_margin_ratio,
            supports_tools=profile.supports_tools,
            supports_streaming=profile.supports_streaming,
            supports_images=profile.supports_images,
            supports_usage=profile.supports_usage,
            token_estimation_method=profile.token_estimation_method,
            provider_extra=profile.provider_extra,
            profile_version=profile.profile_version,
        )

    if pid == "bailian":
        candidate = model_name or cfg.dashscope_model
        profile = profiles.get((pid, candidate))
        if profile is None:
            raise ProviderConfigurationError("百炼模型尚未配置，请使用 deepseek-v4.1-flash")
        endpoint = cfg.dashscope_base_url.strip().rstrip("/")
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.endswith("/compatible-mode/v1")
            or "{" in endpoint
            or "}" in endpoint
        ):
            raise ProviderConfigurationError(
                "请配置百炼控制台的 HTTPS OpenAI 兼容 Base URL（DASHSCOPE_BASE_URL）"
            )
        return profile.model_copy(
            update={
                "base_url": endpoint,
                "context_limit": cfg.dashscope_context_limit,
                "output_reserve": cfg.output_reserve_tokens,
                "request_output_limit": cfg.output_reserve_tokens,
                "recovery_output_limit": cfg.recovery_output_limit_tokens,
                "max_response_recoveries": cfg.max_response_recoveries,
            }
        )

    if model_name is not None:
        profile = profiles.get((pid, model_name))
        if profile is not None:
            return profile

    raise ProviderConfigurationError(
        f"未配置的供应商/模型：provider_id={pid!r} model_name={model_name!r}"
    )


def build_chat_model(
    binding: ModelBinding,
    *,
    secret_store: SecretStore | None = None,
    streaming: bool = True,
) -> ChatOpenAI:
    """构造 LangChain ChatModel。

    密钥通过 `secret_ref` 解析，**不进入 prompt、argv 或日志**。
    解析失败直接抛错，不降级为空字符串。
    """
    store = secret_store or build_secret_store()
    api_key = store.resolve(binding.profile.secret_ref)

    model_class = (
        XiaomiChatOpenAI
        if binding.profile.provider_id == "xiaomi"
        else BailianChatOpenAI
        if binding.profile.provider_id == "bailian"
        else ChatOpenAI
    )
    extra = dict(binding.extra or {})
    if binding.max_retries != 0:
        raise ProviderConfigurationError("SDK 隐藏重试必须关闭，实际尝试由公共调用层观测")
    extra.setdefault("max_tokens", binding.profile.effective_output_limit)
    if (
        int(extra.get("max_completion_tokens", extra["max_tokens"]))
        > binding.profile.effective_output_limit
    ):
        raise ProviderConfigurationError("输出上限不能超过用于请求预算的输出额度")
    if binding.profile.provider_id in {"xiaomi", "bailian"}:
        extra.setdefault("use_responses_api", False)
    if binding.profile.provider_id == "bailian":
        extra.setdefault("stream_usage", True)
    return model_class(
        model=binding.profile.model_name,
        base_url=binding.profile.base_url,
        api_key=SecretStr(api_key),
        temperature=binding.temperature,
        timeout=httpx.Timeout(binding.request_timeout_seconds, connect=15, write=30, pool=15),
        # 关闭 SDK 隐藏重试：重试由本项目的可观测层执行（§11.2）
        max_retries=binding.max_retries,
        streaming=streaming,
        stream_chunk_timeout=binding.request_timeout_seconds,
        **extra,
    )


def request_payload(
    model: Any,
    messages: Sequence[BaseMessage],
    *,
    tools: Sequence[Any] | None = None,
    model_settings: dict[str, Any] | None = None,
    tool_choice: Any = None,
    profile: ProviderProfile | None = None,
) -> dict[str, Any]:
    """使用锁定适配器的实际序列化接缝；不发网络请求，不记录正文。"""
    settings = dict(model_settings or {})
    if tools:
        settings["tools"] = [convert_to_openai_tool(tool) for tool in tools]
        if tool_choice is not None:
            settings["tool_choice"] = tool_choice
    if isinstance(model, ChatOpenAI):
        return model._get_request_payload(messages, **settings)
    wire = [_convert_message_to_dict(message) for message in messages]
    if profile is not None and profile.provider_id in {"xiaomi", "bailian"}:
        from agent_workbench.models.openai_compatible import add_return_fields

        add_return_fields(list(messages), wire)
    return {"messages": wire, **settings}
