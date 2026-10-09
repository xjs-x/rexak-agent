"""供应商适配。

小米走 OpenAI 兼容协议，但**必须单独验证**：完整工具参数、分片工具调用、空响应、
超时、取消、流结束 usage、错误码与图片支持（§11.1）。

控制隐藏 SDK 重试：优先关闭 SDK 内置重试，由单一层做可观测重试；否则明确标记
"请求尝试/费用可能不完整"（§11.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agent_workbench.config.catalog import ProviderProfile, default_provider_profiles
from agent_workbench.config.secrets import SecretStore, build_secret_store
from agent_workbench.config.settings import Settings, get_settings


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
            "profile_version": self.profile.profile_version,
            "token_estimation_method": self.profile.token_estimation_method,
            "supports_tools": self.profile.supports_tools,
            "supports_streaming": self.profile.supports_streaming,
            "supports_usage": self.profile.supports_usage,
            "supports_images": self.profile.supports_images,
            "temperature": self.temperature,
            "max_retries": self.max_retries,
            "secret_ref": self.profile.secret_ref,
        }


def resolve_provider_profile(
    provider_id: str | None = None,
    model_name: str | None = None,
    settings: Settings | None = None,
) -> ProviderProfile:
    """解析供应商档案。默认小米。

    base_url / model 允许被环境变量覆盖（`XIAOMI_BASE_URL` / `XIAOMI_MODEL`），
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
            supports_tools=profile.supports_tools,
            supports_streaming=profile.supports_streaming,
            supports_images=profile.supports_images,
            supports_usage=profile.supports_usage,
            token_estimation_method=profile.token_estimation_method,
            provider_extra=profile.provider_extra,
            profile_version=profile.profile_version,
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

    return ChatOpenAI(
        model=binding.profile.model_name,
        base_url=binding.profile.base_url,
        api_key=SecretStr(api_key),
        temperature=binding.temperature,
        timeout=binding.request_timeout_seconds,
        # 关闭 SDK 隐藏重试：重试由本项目的可观测层执行（§11.2）
        max_retries=binding.max_retries,
        streaming=streaming,
        **(binding.extra or {}),
    )
