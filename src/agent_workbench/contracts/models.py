"""模型设置的只读公开协议，绝不包含密钥值。"""

from pydantic import BaseModel

from agent_workbench.config.catalog import Capability, TokenEstimationMethod


class ModelProfileRead(BaseModel):
    provider_id: str
    model_name: str
    base_url: str
    secret_ref: str
    secret_configured: bool
    context_limit: int
    output_reserve: int
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
