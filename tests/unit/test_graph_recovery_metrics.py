"""A no-update outcome and an unmatched arm must never become a model success."""

import importlib.util
from pathlib import Path

import pytest

PATH = (
    Path(__file__).resolve().parents[2] / "experiments/graph_error_recovery_ab_20261028/metrics.py"
)
spec = importlib.util.spec_from_file_location("graph_recovery_metrics", PATH)
assert spec and spec.loader
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def row(case, arm, *, updates=None, definite=True):
    return {
        "mode": "recovery",
        "case": case,
        "arm": arm,
        "semantic_evaluable": definite,
        "semantic_correct": True if definite else None,
        "conservative_unchanged": not definite,
        "updates": updates or [],
        "first_repair_success": bool(updates) and definite,
        "update_calls": len(updates or []),
        "rejected_updates": 0,
        "get_calls": 1,
        "model_requests": 2,
        "input_tokens": 10,
        "output_tokens": 5,
        "elapsed_seconds": 1.0,
    }


def test_missing_submission_is_not_repair_and_missing_pair_is_not_compared():
    rows = [row("a", "A"), row("a", "B"), row("b", "A")]
    result = metrics.paired_metrics(rows, "recovery")
    assert result["paired_cases"] == ["a"]
    assert result["unpaired_arms"] == 1
    assert result["A"]["first_repair_rate"] == 0
    assert result["A"]["rejection_rate"] is None
    assert result["A"]["model_requests"] == 2


def test_ambiguous_tasks_are_separate_and_duplicates_rejected():
    rows = [row("a", arm, definite=False) for arm in ("A", "B")]
    result = metrics.paired_metrics(rows, "recovery")
    assert result["A"]["definite_tasks"] == 0
    assert result["A"]["first_repair_rate"] is None
    assert result["A"]["conservative_unchanged"] == 1
    with pytest.raises(ValueError, match="Duplicate"):
        metrics.paired_metrics([*rows, rows[0]], "recovery")


def test_read_only_order_review_does_not_rewrite_original_measurement():
    rows = [row("a", arm, definite=False) for arm in ("A", "B")]
    for item in rows:
        item["conservative_unchanged"] = False
        item["semantic_conservative_unchanged"] = True
    result = metrics.paired_metrics(rows, "recovery")
    assert result["A"]["conservative_unchanged"] == 1
    assert result["A"]["first_repair_rate"] is None
    assert rows[0]["conservative_unchanged"] is False
