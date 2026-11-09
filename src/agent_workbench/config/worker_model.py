"""Worker 从 Main 共用模型设置派生运行绑定；凭据不进入任务包或工件。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_workbench.config.model_settings import read_model_key, validate_endpoint
from agent_workbench.config.settings import Settings
from agent_workbench.models.provider import resolve_provider_profile


class WorkerModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["openai-compatible"] = "openai-compatible"
    provider: Literal["bailian", "xiaomi"]
    model: str = Field(min_length=1, max_length=128)
    base_url: str
    api_key: SecretStr
    temperature: float = Field(default=1.0, ge=0, le=2)
    max_tokens: int = Field(gt=0)
    context_window: int = Field(gt=0)
    timeout_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_configuration(self) -> WorkerModelConfig:
        self.base_url = validate_endpoint(self.provider, self.base_url)
        if not self.api_key.get_secret_value().strip() or self.max_tokens >= self.context_window:
            raise ValueError("Worker 密钥或模型预算无效")
        return self

    def transport_value(self) -> dict[str, object]:
        """Only for the trusted local child environment, never log or send via A2A."""
        return {**self.model_dump(mode="json"), "api_key": self.api_key.get_secret_value()}


def worker_model_path(settings: Settings) -> Path:
    return settings.secret_store_path.with_name("ultra-worker-model.json")


def shared_worker_model(settings: Settings, snapshot: dict | None = None) -> WorkerModelConfig:
    """Derive from the Main binding/vault; never persist a second writable model copy."""
    from agent_workbench.config.model_settings import configuration_lock
    from agent_workbench.models.provider import ModelBinding

    with configuration_lock:
        if snapshot is None:
            profile = resolve_provider_profile(settings=settings)
            temperature = 1.0
            timeout = settings.model_request_timeout_seconds
        else:
            binding = ModelBinding.from_snapshot(snapshot)
            profile, temperature, timeout = binding.profile, binding.temperature, binding.request_timeout_seconds
        key = read_model_key(profile.provider_id, settings)
        if not key:
            raise ValueError("请先在模型设置中保存所选供应商的 Key")
        return WorkerModelConfig.model_validate({
            "provider": profile.provider_id, "model": profile.model_name,
            "base_url": profile.base_url, "api_key": key, "temperature": temperature,
            "max_tokens": profile.effective_output_limit, "context_window": profile.context_limit,
            "timeout_ms": timeout * 1000,
        })


def read_worker_model(settings: Settings) -> WorkerModelConfig:
    """Compatibility reader: always use the shared source, ignoring historical snapshots."""
    return shared_worker_model(settings)


def configure_worker_model(settings: Settings, provider: str, model: str) -> None:
    """Compatibility CLI now selects the shared default instead of copying credentials."""
    from agent_workbench.config.model_settings import (
        configuration_lock,
        read_preferences,
        save_preferences,
    )

    with configuration_lock:
        resolve_provider_profile(provider, model, settings)
        if not read_model_key(provider, settings):
            raise ValueError("请先在模型设置中保存对应供应商的 Key")
        preferences = read_preferences(settings)
        preferences.default_provider, preferences.default_model = provider, model
        save_preferences(preferences, settings)
