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

import httpx
from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _convert_message_to_dict
from pydantic import SecretStr, ValidationError

from agent_workbench.config.catalog import ProviderProfile, default_provider_profiles
from agent_workbench.config.model_settings import read_preferences, validate_endpoint
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
    temperature: float = 1.0
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
            "max_output_tokens": self.profile.max_output_tokens,
            "documentation_url": self.profile.documentation_url,
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
                temperature=snapshot.get("temperature", 1.0),
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
    """读取 Web 偏好并解析官方预算档案；未保存 Web 偏好时兼容环境配置。"""
    cfg = settings or get_settings()
    preferences = read_preferences(cfg)
    pid = provider_id or preferences.default_provider
    if pid not in {"xiaomi", "bailian"}:
        raise ProviderConfigurationError("请选择已列出的供应商与模型")
    candidate = model_name or (
        preferences.default_model
        if pid == preferences.default_provider
        else cfg.model_for_provider(pid)
    )
    profile = default_provider_profiles().get((pid, candidate))
    if profile is None:
        raise ProviderConfigurationError("请在 Web 设置中选择已列出的供应商与模型")
    endpoint = preferences.endpoints.get(pid) or (
        cfg.xiaomi_base_url if pid == "xiaomi" else cfg.dashscope_base_url
    )
    try:
        endpoint = validate_endpoint(pid, endpoint)
    except ValueError as exc:
        raise ProviderConfigurationError(str(exc)) from None
    return profile.model_copy(
        update={
            "base_url": endpoint,
            "max_response_recoveries": cfg.max_response_recoveries,
        }
    )


def upgrade_legacy_budget(profile: ProviderProfile) -> ProviderProfile:
    """仅升级旧内置档案的错误预算；模型、端点、凭据引用和运行身份保持原样。"""
    official = default_provider_profiles().get((profile.provider_id, profile.model_name))
    if official is None or profile.profile_version != "1":
        return profile
    return profile.model_copy(
        update={
            key: getattr(official, key)
            for key in (
                "context_limit",
                "max_input_tokens",
                "output_reserve",
                "request_output_limit",
                "recovery_output_limit",
                "max_output_tokens",
                "profile_version",
                "documentation_url",
            )
        }
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
