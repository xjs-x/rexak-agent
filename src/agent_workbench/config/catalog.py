"""模型供应商档案与价格目录。

对未知能力一律使用 `unknown`，**不从模型名字推断**（BUILD_PLAN §11.1）。
价格使用 `Decimal`，每个模型调用保存价格快照或版本号（§11.3）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

PRICE_CATALOG_VERSION = "2026-10-08.1"

Capability = Literal["supported", "unsupported", "unknown"]

TokenEstimationMethod = Literal[
    "provider_tokenizer",
    "tiktoken_compatible",
    "chars_div_4",
    "unknown",
]


class ProviderProfile(BaseModel):
    """一个供应商/模型的静态能力与配置快照。"""

    model_config = {"frozen": True}

    provider_id: str
    model_name: str
    base_url: str
    secret_ref: str
    context_limit: int
    output_reserve: int = 8192
    budget_safety_margin_ratio: float = Field(default=0.1, ge=0, lt=1)
    supports_tools: Capability = "unknown"
    supports_streaming: Capability = "unknown"
    supports_images: Capability = "unknown"
    supports_usage: Capability = "unknown"
    token_estimation_method: TokenEstimationMethod = "unknown"
    provider_extra: dict[str, object] = Field(default_factory=dict)
    profile_version: str = "1"

    @property
    def input_budget_tokens(self) -> int:
        return max(0, self.context_limit - self.output_reserve)


class ModelPrice(BaseModel):
    """一个计费单元的单价。`unit` 之外一律按每 1M token 计价。"""

    model_config = {"frozen": True}

    provider_id: str
    model_name: str
    currency: str
    effective_at: str
    unit: Literal["per_1m_tokens", "per_request"] = "per_1m_tokens"
    input_per_unit: Decimal = Decimal("0")
    output_per_unit: Decimal = Decimal("0")
    cache_read_per_unit: Decimal | None = None
    cache_write_per_unit: Decimal | None = None
    #: 该供应商是否把缓存 token 计入 input_tokens（决定是否需要扣除，§11.3 规则 1）
    cache_tokens_included_in_input: bool = False
    #: 该供应商是否把推理 token 计入 output_tokens（§11.3 规则 1）
    reasoning_tokens_included_in_output: bool = False


class PriceCatalog(BaseModel):
    """不可变的价格目录快照。"""

    model_config = {"frozen": True}

    version: str
    prices: tuple[ModelPrice, ...] = ()

    def find(self, provider_id: str, model_name: str) -> ModelPrice | None:
        for price in self.prices:
            if price.provider_id == provider_id and price.model_name == model_name:
                return price
        return None


def default_provider_profiles() -> dict[tuple[str, str], ProviderProfile]:
    """版本化默认供应商档案。

    小米走 OpenAI 兼容协议适配。**能力字段全部为 `unknown`**，
    必须由 `agent_workbench.models.probe` 用真实请求探明后再写入决策记录，
    不允许按模型名字声称支持工具调用或流式。
    """

    return {
        ("xiaomi", "mimo-v2.6-pro"): ProviderProfile(
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            base_url="https://api.xiaomimimo.com/v1",
            secret_ref="env:XIAOMI_API_KEY",
            context_limit=131072,
            output_reserve=8192,
            token_estimation_method="chars_div_4",
            provider_extra={"protocol": "openai-compatible"},
        ),
    }


def default_price_catalog() -> PriceCatalog:
    """版本化默认价格目录。

    首版**不臆造**小米单价：目录为空时模型调用以 `unpriced` 状态记录 tokens
    而不显示费用为 0（ACCEPTANCE A28）。用户可在设置中写入真实价格后，
    新 run 使用新价格版本。
    """

    return PriceCatalog(version=PRICE_CATALOG_VERSION)
