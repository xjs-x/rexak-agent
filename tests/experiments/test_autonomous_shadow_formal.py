"""新冻结正式评价：仅显式启用时运行一次，隔离真实 PostgreSQL，保存预测后读标签。"""

from __future__ import annotations

import json
import os
import time

import pytest
from experiments.history_action_autonomous_shadow_v1.evaluate import ROOT, verify, write
from experiments.history_action_trusted_shadow_v1.fixture import row_dump
from tests.integration.test_autonomous_shadow import run_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("REXAK_AUTONOMOUS_FORMAL") != "1",
        reason="formal evaluation not enabled; never rerun completed ATTEMPT",
    ),
]


async def test_frozen_autonomous_runner_evaluation_once(database, tmp_path, monkeypatch):
    write(ROOT / "ATTEMPT.json", {"status": "STARTED_ONCE", **verify()})
    scenarios = json.loads((ROOT / "frozen/scenarios.json").read_text(encoding="utf-8"))
    predictions = []
    for spec in scenarios:
        start = time.perf_counter()
        result = await run_case(
            database,
            "autonomous-evaluation-" + spec["case"],
            tmp_path / spec["case"],
            monkeypatch,
            spec=spec,
        )
        observations = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (result["shadow_root"] / "observations").glob("*.json")
        ]
        current = [o for o in observations if o["candidate_tool_call_id"] == "repeat"]
        observation = current[0] if len(current) == 1 else None
        captures = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (result["shadow_root"] / "captures").glob("*.json")
        ]
        assert len(result["submissions"]) == 2
        write(
            ROOT / "results/traces" / (spec["case"] + ".json"),
            {
                "source": "PRODUCTION_RUNNER_POSTGRES_MOCK_EXECUTOR",
                "operations": [row_dump(o) for o in result["operations"]],
                "jobs": [row_dump(j) for j in result["jobs"]],
                "artifacts": [
                    row_dump(a) for a in result["artifacts"] if a.source_operation_id is not None
                ],
                "captures": captures,
                "observations": observations,
                "dispatches": result["submissions"],
            },
        )
        predictions.append(
            {
                "case": spec["case"],
                "observation": observation,
                "triggered": bool(observation and observation["emit_shadow_event"]),
                "candidate_capture": any(c["tool_call_id"] == "repeat" for c in captures),
                "history_capture": any(c["tool_call_id"] == "first" for c in captures),
                "physical_jobs": len(result["submissions"]),
                "elapsed_ms": (time.perf_counter() - start) * 1000,
                "observation_bytes": sum(
                    p.stat().st_size for p in result["shadow_root"].rglob("*.json")
                ),
            }
        )
    write(ROOT / "results/predictions.json", predictions)
    # 预测全部写入之后才读取 Oracle。
    labels = json.loads((ROOT / "frozen/labels.json").read_text(encoding="utf-8"))
    lookup = {v["case"]: v for v in predictions}
    matrix = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for label in labels:
        if label["bucket"] != "COMPARABLE":
            continue
        p = lookup[label["case"]]["triggered"]
        matrix[
            "tp"
            if p and label["expected_trigger"]
            else "fp"
            if p
            else "fn"
            if label["expected_trigger"]
            else "tn"
        ] += 1
    write(
        ROOT / "results/metrics.json",
        {
            "source": "PRODUCTION_RUNNER_POSTGRES_MOCK_EXECUTOR",
            "total": len(predictions),
            "matrix": matrix,
            "comparable_denominator": sum(matrix.values()),
            "candidate_complete": sum(p["candidate_capture"] for p in predictions),
            "trigger_events": sum(p["triggered"] for p in predictions),
            "unsupported_and_navigation_not_counted_as_tn": True,
            "real_provider_requests": 0,
            "real_executor_requests": 0,
        },
    )
    write(ROOT / "results/COMPLETE.json", {"status": "COMPLETED_ONCE", **verify()})
