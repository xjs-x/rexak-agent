"""Worker 独立模型快照与本机密钥。不会随 A2A、业务工件或主 Agent 设置流转。"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agent_workbench.config.model_settings import read_model_key, validate_endpoint
from agent_workbench.config.secrets import write_secret_file
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


def read_worker_model(settings: Settings) -> WorkerModelConfig | None:
    path = worker_model_path(settings)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("format") == "dpapi" and sys.platform == "win32":
        from agent_workbench.config.model_settings import _dpapi

        payload = json.loads(_dpapi(base64.b64decode(value["payload"]), decrypt=True))
    elif value.get("format") == "local" and sys.platform != "win32":
        payload = value["payload"]
    else:
        raise ValueError("Worker 模型配置不能由当前用户解密")
    return WorkerModelConfig.model_validate(payload)


def configure_worker_model(settings: Settings, provider: str, model: str) -> None:
    """Explicitly copy an existing provider binding once; later Main changes do not affect Worker."""
    profile = resolve_provider_profile(provider, model, settings)
    key = read_model_key(provider, settings)
    if not key:
        raise ValueError("请先保存对应供应商的模型密钥")
    value = WorkerModelConfig.model_validate({
        "provider": provider, "model": model, "base_url": profile.base_url,
        "api_key": key, "max_tokens": profile.effective_output_limit,
        "context_window": profile.context_limit,
        "timeout_ms": settings.model_request_timeout_seconds * 1000,
    }).transport_value()
    if sys.platform == "win32":
        from agent_workbench.config.model_settings import _dpapi

        protected = _dpapi(json.dumps(value).encode("utf-8"))
        stored = {"format": "dpapi", "payload": base64.b64encode(protected).decode("ascii")}
    else:
        stored = {"format": "local", "payload": value}
    write_secret_file(worker_model_path(settings), stored)
