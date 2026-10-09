"""模型调用用量与费用契约（BUILD_PLAN §11.3）。

关键规则（摘要）：
1. 缓存 token 可能已包含在 `input_tokens` 中，推理 token 可能包含在 `output_tokens` 中，
   按供应商口径拆分，**不能直接全部相加**；
2. stream usage 可能是累计值，完成时还会再给总量；同一 `model_call_id` 更新结算，
   **不能逐块累加再加一次总量**；
3. 重试的每一次可能计费；取消或超时**不自动等于零费用**；
4. 没有价格但有用量 → 显示 tokens 与 `unpriced`；
5. 不同货币不直接相加；
6. 费用台账是应用侧估算/计算，**不冒充供应商结算账单**。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.contracts.enums import CostStatus, UsageStatus


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceSnapshot(_Base):
    """写入 `model_calls` 的价格快照。乐观时留空表示 unpriced。"""

    catalog_version: str
    currency: str | None = None
    unit: str | None = None
    input_per_unit: Decimal | None = None
    output_per_unit: Decimal | None = None
    cache_read_per_unit: Decimal | None = None
    cache_write_per_unit: Decimal | None = None
    cache_tokens_included_in_input: bool = False
    reasoning_tokens_included_in_output: bool = False


class UsageUpdate(_Base):
    """一次用量更新。`semantics` 明确增量 / 累计 / 最终语义。"""

    semantics: Literal["incremental", "cumulative", "final"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    status: UsageStatus = UsageStatus.PENDING


class ModelCallUsage(_Base):
    """一次**物理**模型请求尝试的台账行。

    以"一次真实网络尝试"为单位生成 `model_call_id`；
    重试生成新的 `model_call_id`，共享同一 `logical_call_id`（§11.2）。
    """

    logical_call_id: UUID
    model_call_id: UUID
    run_id: UUID
    session_id: UUID
    phase: str
    retry_index: int = 0
    provider_id: str
    model_name: str
    request_id: str | None = None
    #: 供应商可能隐藏重试；无法观测时标记请求尝试/费用可能不完整
    attempts_observable: bool = True
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    usage_status: UsageStatus = UsageStatus.PENDING
    cost_status: CostStatus = CostStatus.UNKNOWN
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    price_snapshot: PriceSnapshot | None = None
    submitted_at: datetime | None = None
    first_token_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None


class UsageSummary(_Base):
    """按 run/session 聚合的展示视图。

    未知请求数单列；**已知小计不冒充完整总费用**（ACCEPTANCE A28）。
    """

    currency: str
    priced_calls: int = 0
    unpriced_calls: int = 0
    unknown_usage_calls: int = 0
    known_cost: Decimal = Decimal("0")
    estimated_cost: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
