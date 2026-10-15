"""模型设置的只读公开协议，绝不包含密钥值。"""

from typing import Any

from pydantic import BaseModel, Field, SecretStr

from agent_workbench.config.catalog import Capability, TokenEstimationMethod


class ModelProfileRead(BaseModel):
    provider_id: str
    model_name: str
    base_url: str
    secret_ref: str
    secret_configured: bool
    context_limit: int
    output_reserve: int
    request_output_limit: int
    allowed_input_tokens: int
    max_output_tokens: int
    documentation_url: str | None = None
    supports_tools: Capability
    supports_streaming: Capability
    supports_images: Capability
    supports_usage: Capability
    token_estimation_method: TokenEstimationMethod
    profile_version: str


class ModelSettingsRead(BaseModel):
    default_provider: str
    default_model: str
    profiles: list[ModelProfileRead]
    price_catalog_version: str
    effective_scope: str


class ModelSettingsUpdate(BaseModel):
    provider_id: str
    model_name: str
    base_url: str
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)
    delete_api_key: bool = False
    set_default: bool = True


class RunSummaryRead(BaseModel):
    id: str
    provider_id: str
    model_name: str
    status: str
    completion_reason: str | None
    objective: str
    control_version: int
    stop_reason: str | None = None
    owner_generation: int
    created_at: str


class SessionSnapshotRead(BaseModel):
    session: dict[str, Any]
    runs: list[RunSummaryRead]
    leads: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    coverage: dict[str, Any]
    event_cursor: int
