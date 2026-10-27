"""费用计算。

硬规则（§11.3）：
1. 缓存 token 可能已包含在 `input_tokens` 中，推理 token 可能包含在 `output_tokens` 中，
   按供应商口径拆分，**不能直接全部相加**；
2. stream usage 可能是累计值，完成时还会给总量：同一 `model_call_id` **幂等更新**，
   不逐块累加再加一次总量；
3. 重试的每一次可能计费；取消或超时**不自动等于零费用**；
4. 没有价格但有用量 -> 显示 tokens 与 `unpriced`；
5. 不同货币**不直接相加**；
6. 费用台账是**应用侧估算/计算**，不冒充供应商结算账单。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agent_workbench.config.catalog import ModelPrice, PriceCatalog
from agent_workbench.contracts.enums import CostStatus, UsageStatus
from agent_workbench.contracts.usage import PriceSnapshot, UsageUpdate

_ONE_MILLION = Decimal(1_000_000)


@dataclass(slots=True)
class UsageTotals:
    """按供应商口径归一化后的用量。

    `billable_input` / `billable_output` 已经按"缓存/推理是否包含在总量中"做了拆分。
    """

    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    raw_usage: dict[str, Any]
    usage_status: UsageStatus


def normalize_usage(raw: dict[str, Any]) -> UsageTotals:
    """把供应商返回的 usage 归一化。

    兼容常见字段名，**保留原始字段**在 `raw_usage` 中供核对（§11.2）。
    """
    if not raw:
        return UsageTotals(None, None, None, None, None, {}, UsageStatus.UNKNOWN)

    def pick(*names: str) -> int | None:
        for name in names:
            value = _dig(raw, name)
            if value is not None:
                return value
        return None

    input_tokens = pick("input_tokens", "prompt_tokens", "input_token_count")
    output_tokens = pick("output_tokens", "completion_tokens", "output_token_count")
    cache_read = pick(
        # 小米 mimo 实测返回 input_token_details.cache_read；其余为常见兼容字段
        "input_token_details.cache_read",
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
        "prompt_tokens_details.cached_tokens",
    )
    cache_write = pick(
        "input_token_details.cache_write",
        "cache_write_tokens",
        "cache_creation_input_tokens",
    )
    reasoning = pick(
        # 小米 mimo 实测返回 output_token_details.reasoning
        "output_token_details.reasoning",
        "reasoning_tokens",
        "reasoning_output_tokens",
        "completion_tokens_details.reasoning_tokens",
    )

    if input_tokens is None and output_tokens is None:
        status = UsageStatus.UNKNOWN
    elif cache_read is None and cache_write is None and reasoning is None:
        status = UsageStatus.REPORTED
    else:
        status = UsageStatus.REPORTED
    return UsageTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        raw_usage=raw,
        usage_status=status,
    )


def _dig(payload: dict[str, Any], path: str) -> int | None:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if isinstance(current, bool):
        return None
    if isinstance(current, (int, float)):
        return int(current)
    return None


def merge_usage_update(
    previous: UsageTotals,
    update: UsageUpdate,
) -> UsageTotals:
    """按语义合并一次用量更新。

    - `incremental`：累加；
    - `cumulative`：**替换**（流式累计值）；
    - `final`：**替换**为最终值。

    这正是"不能逐块累加再加一次总量"的实现点。
    """
    if update.semantics == "incremental":
        return UsageTotals(
            input_tokens=_add(previous.input_tokens, update.input_tokens),
            output_tokens=_add(previous.output_tokens, update.output_tokens),
            cache_read_tokens=_add(previous.cache_read_tokens, update.cache_read_tokens),
            cache_write_tokens=_add(previous.cache_write_tokens, update.cache_write_tokens),
            reasoning_tokens=_add(previous.reasoning_tokens, update.reasoning_tokens),
            raw_usage={**previous.raw_usage, **update.raw_usage},
            usage_status=update.status,
        )
    return UsageTotals(
        input_tokens=update.input_tokens
        if update.input_tokens is not None
        else previous.input_tokens,
        output_tokens=update.output_tokens
        if update.output_tokens is not None
        else previous.output_tokens,
        cache_read_tokens=update.cache_read_tokens
        if update.cache_read_tokens is not None
        else previous.cache_read_tokens,
        cache_write_tokens=update.cache_write_tokens
        if update.cache_write_tokens is not None
        else previous.cache_write_tokens,
        reasoning_tokens=update.reasoning_tokens
        if update.reasoning_tokens is not None
        else previous.reasoning_tokens,
        raw_usage={**previous.raw_usage, **update.raw_usage},
        usage_status=update.status,
    )


def _add(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


@dataclass(slots=True)
class CostComputation:
    status: CostStatus
    amount: Decimal | None
    currency: str | None
    snapshot: PriceSnapshot | None
    detail: dict[str, Any]


def compute_cost(
    totals: UsageTotals,
    price: ModelPrice | None,
    catalog: PriceCatalog,
) -> CostComputation:
    """用 Decimal 计算费用。

    - 无价格但有用量 -> `unpriced`（**仍然返回 tokens**，不显示为 0）；
    - 用量缺失 -> `unknown`；
    - 供应商把缓存/推理 token 包含在总量里时，按单价差额补偿而不是重复计价。
    """
    snapshot = (
        PriceSnapshot(
            catalog_version=catalog.version,
            currency=price.currency,
            unit=price.unit,
            input_per_unit=price.input_per_unit,
            output_per_unit=price.output_per_unit,
            cache_read_per_unit=price.cache_read_per_unit,
            cache_write_per_unit=price.cache_write_per_unit,
            cache_tokens_included_in_input=price.cache_tokens_included_in_input,
            reasoning_tokens_included_in_output=price.reasoning_tokens_included_in_output,
        )
        if price is not None
        else PriceSnapshot(catalog_version=catalog.version)
    )

    if totals.usage_status is UsageStatus.UNKNOWN and totals.input_tokens is None:
        return CostComputation(CostStatus.UNKNOWN, None, None, snapshot, {"reason": "无用量数据"})

    if price is None:
        return CostComputation(
            CostStatus.UNPRICED,
            None,
            None,
            snapshot,
            {
                "reason": "价格目录中没有该模型的价格",
                "note": "仍记录 tokens；不要显示为 0 成本。",
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
            },
        )

    billable_input = Decimal(totals.input_tokens or 0)
    billable_output = Decimal(totals.output_tokens or 0)
    cache_read = Decimal(totals.cache_read_tokens or 0)
    cache_write = Decimal(totals.cache_write_tokens or 0)

    detail: dict[str, Any] = {
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_tokens": totals.cache_read_tokens,
        "cache_write_tokens": totals.cache_write_tokens,
        "reasoning_tokens": totals.reasoning_tokens,
    }

    if price.cache_tokens_included_in_input and cache_read:
        # 缓存命中 token 已计入 input，按差额补足，不重复计价
        effective = price.cache_read_per_unit
        if effective is None:
            detail["cache_pricing"] = "供应商把缓存计入 input 且未单独定价，按 input 单价计"
        else:
            billable_input -= cache_read
            detail["cache_pricing"] = "缓存命中 token 已从 input 中扣除，按 cache_read 单价单独计"

    total = (
        billable_input * price.input_per_unit
        + billable_output * price.output_per_unit
        + cache_read * (price.cache_read_per_unit or Decimal(0))
        + cache_write * (price.cache_write_per_unit or Decimal(0))
    ) / _ONE_MILLION

    return CostComputation(
        CostStatus.CALCULATED,
        total.quantize(Decimal("0.0000000001")),
        price.currency,
        snapshot,
        detail,
    )


def summarize_costs(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """按货币分别汇总。

    **不同货币不直接相加**；未知请求数与已知小计分开显示（ACCEPTANCE A28）。
    """
    by_currency: dict[str, dict[str, Any]] = {}
    unpriced_calls = 0
    unknown_usage_calls = 0
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )
    token_totals: dict[str, int | None] = dict.fromkeys(token_fields)

    for row in rows:
        for field in token_fields:
            value = row.get(field)
            if value is not None:
                token_totals[field] = (token_totals[field] or 0) + int(value)
        status = row.get("cost_status")
        usage_status = row.get("usage_status")
        if usage_status in {str(UsageStatus.UNKNOWN), None}:
            unknown_usage_calls += 1
        if status == str(CostStatus.UNPRICED):
            unpriced_calls += 1
            continue
        if status != str(CostStatus.CALCULATED):
            continue
        currency = row.get("cost_currency") or "UNKNOWN"
        bucket = by_currency.setdefault(
            currency,
            {
                "currency": currency,
                "known_cost": Decimal(0),
                "priced_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        amount = row.get("cost_amount")
        if amount is not None:
            bucket["known_cost"] += Decimal(str(amount))
        bucket["priced_calls"] += 1
        bucket["input_tokens"] += int(row.get("input_tokens") or 0)
        bucket["output_tokens"] += int(row.get("output_tokens") or 0)
        bucket["cache_read_tokens"] += int(row.get("cache_read_tokens") or 0)
        bucket["cache_write_tokens"] += int(row.get("cache_write_tokens") or 0)
        bucket["reasoning_tokens"] += int(row.get("reasoning_tokens") or 0)

    return {
        "by_currency": {
            currency: {**bucket, "known_cost": str(bucket["known_cost"])}
            for currency, bucket in by_currency.items()
        },
        "unpriced_calls": unpriced_calls,
        "unknown_usage_calls": unknown_usage_calls,
        "token_totals": token_totals,
        "note": (
            "这是应用侧按价格目录计算的结果，不等于供应商结算账单；"
            "未知用量请求单列，不能当作 0 成本参与平均值。"
        ),
    }
