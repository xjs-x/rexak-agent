"""模型设置的只读公开协议，绝不包含密钥值。"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, StringConstraints, model_validator

from agent_workbench.config.catalog import Capability, TokenEstimationMethod
from agent_workbench.contracts.delegation import ExecutionMode

ProviderID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")]
ModelID = Annotated[str, StringConstraints(pattern=r"^[^\s\x00-\x1f\x7f]{1,128}$")]
TokenCount = Annotated[int, Field(gt=0, le=2_147_483_647)]
ModelProtocol = Literal["openai-chat-completions"]
DiscoveryFormat = Literal["openai", "dashscope"]


class CatalogModel(BaseModel):
    model_id: ModelID
    display_name: str = Field(default="", max_length=160)
    enabled: bool = True
    context_window: TokenCount | None = None
    max_input_tokens: TokenCount | None = None
    max_output_tokens: TokenCount | None = None
    request_output_limit: TokenCount | None = None
    supports_reasoning: Capability = "unknown"
    supports_tools: Capability = "unknown"
    supports_images: Capability = "unknown"
    specification_source: Literal["unknown", "user", "provider_catalog", "built_in"] = "unknown"

    @model_validator(mode="after")
    def check_budgets(self):
        limit = self.request_output_limit
        if self.context_window is not None:
            if self.context_window < 2:
                raise ValueError("上下文窗口至少为 2 tokens")
            if limit is not None and limit >= self.context_window:
                raise ValueError("请求输出预算必须小于上下文窗口")
            if self.max_input_tokens is not None and self.max_input_tokens > self.context_window:
                raise ValueError("最大输入长度不能超过上下文窗口")
            if self.max_output_tokens is not None and self.max_output_tokens > self.context_window:
                raise ValueError("最大输出长度不能超过上下文窗口")
        if (
            limit is not None
            and self.max_output_tokens is not None
            and limit > self.max_output_tokens
        ):
            raise ValueError("请求输出预算不能超过模型最大输出")
        return self


class ModelConnection(BaseModel):
    protocol: ModelProtocol = "openai-chat-completions"
    base_url: str = Field(max_length=2048)
    models_url: str | None = Field(default=None, max_length=2048)
    discovery_format: DiscoveryFormat = "openai"


class ProviderDraft(ModelConnection):
    provider_id: ProviderID
    display_name: str = Field(min_length=1, max_length=160, pattern=r"\S")
    enabled: bool = True
    models: list[CatalogModel] = Field(default_factory=list, max_length=3000)


class ProviderRead(ProviderDraft):
    secret_configured: bool


class ProviderWrite(ProviderDraft):
    revision: int
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)
    delete_api_key: bool = False
    default_model: ModelID | None = None


class ModelDiscoveryRequest(ModelConnection):
    saved_provider_id: ProviderID | None = None
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)


class ModelDiscoveryRead(BaseModel):
    models: list[CatalogModel]
    truncated: bool = False


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
    max_output_tokens: int | None
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
    providers: list[ProviderRead] = Field(default_factory=list)
    revision: int = 0


class ModelSettingsUpdate(BaseModel):
    provider_id: str
    model_name: str
    base_url: str
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)
    delete_api_key: bool = False
    set_default: bool = True


class RunSummaryRead(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.STANDARD
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
    model_response_saved: bool = False


class SessionSnapshotRead(BaseModel):
    session: dict[str, Any]
    runs: list[RunSummaryRead]
    leads: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    coverage: dict[str, Any]
    event_cursor: int
