"""隔离 PostgreSQL/完整 Runner 的第五阶段非干预回归；不运行旧正式评价。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from experiments.history_action_autonomous_shadow_v1.projections import business_view, differences
from experiments.history_action_reflection_preparation_v1.benchmark import (
    ROOT,
    original_service,
    write,
)

from agent_workbench.tools import history_action_shadow as active
from tests.integration.test_autonomous_shadow import run_case

pytestmark = pytest.mark.integration


async def test_stage4_scenarios_old_and_optimized_runner_semantics(database, unique_name, tmp_path, monkeypatch):
    source = ROOT.parent / "history_action_autonomous_shadow_v1/frozen/scenarios.json"
    specs = json.loads(source.read_text(encoding="utf-8"))
    baseline, optimized = original_service(), active.ShadowService
    rows = []
    for spec in specs:
        results = {}
        for arm, service in (("stage4", baseline), ("optimized", optimized)):
            monkeypatch.setattr(active, "ShadowService", service)
            results[arm] = await run_case(database, unique_name + spec["case"] + arm,
                                          tmp_path / (spec["case"] + arm), monkeypatch, spec=spec)
        views = [cast(dict[str, Any], business_view(results[a])) for a in ("stage4", "optimized")]
        before = differences(views[0], views[1])
        # 业务表 SELECT 未承诺顺序；完整行作多重集比较，模型消息/派发/工件引用数组顺序不变。
        for view in views:
            for name in ("http_artifacts", "operations", "jobs"):
                view[name].sort(key=lambda row: json.dumps(row, sort_keys=True))
        changes = differences(views[0], views[1])
        diagnostic = ROOT / ("verification/multiset-" + spec["case"] + ".json")
        if not diagnostic.exists():
            write(diagnostic, {"case": spec["case"], "before": before, "after": changes, "views": views})
        assert changes == [], changes
        assert len(results["stage4"]["events"]) == len(results["optimized"]["events"])
        observed = []
        for arm in ("stage4", "optimized"):
            values = [json.loads(p.read_text(encoding="utf-8")) for p in (results[arm]["shadow_root"] / "observations").glob("*.json")]
            observed.append(next(o for o in values if o.get("candidate_tool_call_id") == "repeat"))
        assert observed[0]["classification"] == observed[1]["classification"]
        assert observed[0]["emit_shadow_event"] == observed[1]["emit_shadow_event"]
        rows.append({"case": spec["case"], "business_differences": [], "classification": observed[1]["classification"],
                     "trigger": observed[1]["emit_shadow_event"], "event_counts": [len(results[a]["events"]) for a in ("stage4", "optimized")]})
    path = ROOT / "verification/runner-stage4-comparison.json"
    if not path.exists():
        write(path, {"source": "SEEN_16_SCENARIOS_NEW_RUNS_REGRESSION_NOT_BLIND", "rows": rows})


async def test_postgres_query_phase_diagnostic(database, unique_name, tmp_path, monkeypatch):
    """补充真实 SQL 时点采样，两个新 Run；不替换内存负载性能或正式准确率。"""
    baseline, optimized = original_service(), active.ShadowService
    rows = []
    for arm, service in (("stage4", baseline), ("optimized", optimized)):
        monkeypatch.setattr(active, "ShadowService", service)
        result = await run_case(database, unique_name + arm, tmp_path / arm, monkeypatch)
        for source in (result["shadow_root"] / "observations").glob("*.json"):
            observation = json.loads(source.read_text(encoding="utf-8"))
            rows.append({"arm": arm, "tool_call_id": observation["candidate_tool_call_id"],
                         "classification": observation["classification"],
                         "timing_ms": observation.get("timing_ms", {})})
    assert len(rows) == 4
    assert all("history_query_ms" in row["timing_ms"] for row in rows)
    path = ROOT / "verification/postgres-direct-phase-diagnostic.json"
    if not path.exists():
        write(path, {"source": "LOOPBACK_RANDOM_TEST_DATABASE_REAL_SQL_MOCK_EXECUTOR", "rows": rows,
                     "repetitions_per_arm": 1, "not_tail_or_throughput_claim": True})
