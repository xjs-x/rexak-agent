"""费用与用量口径。

直接对应 BUILD_PLAN §11.3 的七条规则与 ACCEPTANCE A27/A28。
"""

from __future__ import annotations

from decimal import Decimal

from agent_workbench.config.catalog import ModelPrice, PriceCatalog
from agent_workbench.contracts.enums import CostStatus, UsageStatus
from agent_workbench.contracts.usage import UsageUpdate
from agent_workbench.observability.costs import (
    compute_cost,
    merge_usage_update,
    normalize_usage,
    summarize_costs,
)


def _catalog(**kwargs: object) -> PriceCatalog:
    price = ModelPrice(
        provider_id="xiaomi",
        model_name="mimo-v2.6-pro",
        currency="CNY",
        effective_at="2026-10-08",
        input_per_unit=Decimal("1.0"),
        output_per_unit=Decimal("2.0"),
        cache_read_per_unit=Decimal("0.1"),
        cache_tokens_included_in_input=bool(kwargs.get("cache_included", True)),
    )
    return PriceCatalog(version="test-1", prices=(price,))


def test_normalize_reads_mimo_field_paths() -> None:
    """小米实测字段路径：input_token_details.cache_read / output_token_details.reasoning。"""
    totals = normalize_usage(
        {
            "input_tokens": 144,
            "output_tokens": 49,
            "input_token_details": {"cache_read": 32},
            "output_token_details": {"reasoning": 23},
        }
    )
    assert totals.input_tokens == 144
    assert totals.output_tokens == 49
    assert totals.cache_read_tokens == 32
    assert totals.reasoning_tokens == 23
    assert totals.usage_status is UsageStatus.REPORTED


def test_normalize_keeps_raw_for_reconciliation() -> None:
    raw = {"input_tokens": 1, "provider_specific": {"x": 1}}
    assert normalize_usage(raw).raw_usage == raw


def test_absent_usage_is_unknown_not_zero() -> None:
    totals = normalize_usage({})
    assert totals.usage_status is UsageStatus.UNKNOWN
    assert totals.input_tokens is None
    cost = compute_cost(totals, None, PriceCatalog(version="t"))
    assert cost.status is CostStatus.UNKNOWN
    assert cost.amount is None


def test_usage_without_price_is_unpriced_but_keeps_tokens() -> None:
    totals = normalize_usage({"input_tokens": 10, "output_tokens": 5})
    cost = compute_cost(totals, None, PriceCatalog(version="empty"))
    assert cost.status is CostStatus.UNPRICED
    assert cost.amount is None
    assert cost.detail["input_tokens"] == 10
    assert "不要显示为 0" in cost.detail["note"]


def test_cumulative_stream_usage_replaces_instead_of_accumulating() -> None:
    """流式 usage 是累计值时，完成时的总量必须**替换**而不是相加。"""
    previous = normalize_usage({"input_tokens": 10, "output_tokens": 3})
    after_chunk = merge_usage_update(
        previous,
        UsageUpdate(semantics="cumulative", input_tokens=10, output_tokens=40),
    )
    after_final = merge_usage_update(
        after_chunk,
        UsageUpdate(semantics="final", input_tokens=10, output_tokens=55),
    )
    assert after_final.output_tokens == 55, "不能得到 3+40+55"


def test_incremental_usage_accumulates() -> None:
    previous = normalize_usage({"input_tokens": 10, "output_tokens": 3})
    merged = merge_usage_update(
        previous, UsageUpdate(semantics="incremental", output_tokens=4, status=UsageStatus.PARTIAL)
    )
    assert merged.output_tokens == 7


def test_cache_tokens_included_in_input_are_not_double_billed() -> None:
    totals = normalize_usage(
        {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_token_details": {"cache_read": 400_000},
        }
    )
    cost = compute_cost(totals, _catalog(cache_included=True).prices[0], _catalog())
    expected = Decimal(600_000) * Decimal("1.0") / Decimal(1_000_000) + Decimal(400_000) * Decimal(
        "0.1"
    ) / Decimal(1_000_000)
    assert cost.amount == expected.quantize(Decimal("0.0000000001"))


def test_costs_are_not_summed_across_currencies() -> None:
    summary = summarize_costs(
        [
            {
                "cost_status": str(CostStatus.CALCULATED),
                "usage_status": str(UsageStatus.REPORTED),
                "cost_amount": Decimal("1.5"),
                "cost_currency": "CNY",
                "input_tokens": 10,
                "output_tokens": 1,
            },
            {
                "cost_status": str(CostStatus.CALCULATED),
                "usage_status": str(UsageStatus.REPORTED),
                "cost_amount": Decimal("2.0"),
                "cost_currency": "USD",
                "input_tokens": 20,
                "output_tokens": 2,
            },
        ]
    )
    assert set(summary["by_currency"]) == {"CNY", "USD"}
    assert summary["by_currency"]["CNY"]["known_cost"] == "1.5"
    assert summary["by_currency"]["USD"]["known_cost"] == "2.0"


def test_summary_separates_unknown_and_unpriced() -> None:
    summary = summarize_costs(
        [
            {
                "cost_status": str(CostStatus.UNPRICED),
                "usage_status": str(UsageStatus.REPORTED),
                "input_tokens": 5,
            },
            {
                "cost_status": str(CostStatus.UNKNOWN),
                "usage_status": str(UsageStatus.UNKNOWN),
            },
        ]
    )
    assert summary["unpriced_calls"] == 1
    assert summary["unknown_usage_calls"] == 1
    assert "不能当作 0 成本" in summary["note"]
