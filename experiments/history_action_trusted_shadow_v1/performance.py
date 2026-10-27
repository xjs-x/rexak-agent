"""使用开发夹具测成本；密集历史克隆是 SYNTHETIC_LOAD，不计评价样本。"""

from __future__ import annotations

import asyncio
import copy
import json
import statistics
import time
import tracemalloc
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from experiments.history_action_trusted_shadow_v1.dataset import generate, predict
from experiments.history_action_trusted_shadow_v1.fixture import (
    approve,
    graph_run,
    make_toolbox,
    tool_arguments,
)

from agent_workbench.domain.history_action_reflection import digest

ROOT = Path(__file__).resolve().parent


async def main():
    frame = await generate(ROOT / "performance-data" / "seed", "development-cost")
    rows = []
    original = next(op for op in frame["operations"] if op["tool_call_id"] == "seed")
    for size in (1, 100, 500):
        value = copy.deepcopy(frame)
        for index in range(size - 1):
            oid = str(uuid5(NAMESPACE_URL, f"bench-op-{index}"))
            op = copy.deepcopy(original)
            op["operation_id"] = oid
            op["tool_call_id"] = f"bench-{index}"
            op["result_artifact_ids"] = []
            value["operations"].append(op)
            recorded = copy.deepcopy(frame["captures"][original["operation_id"]])
            recorded["operation_id"] = oid
            recorded["tool_call_id"] = op["tool_call_id"]
            recorded["approval"]["tool_call_id"] = op["tool_call_id"]
            recorded["approval_sha256"] = digest(recorded["approval"])
            value["captures"][oid] = recorded
            for j in frame["jobs"]:
                job = copy.deepcopy(j)
                job["operation_id"] = oid
                job["job_id"] = str(uuid5(NAMESPACE_URL, f"bench-job-{index}"))
                value["jobs"].append(job)
        timing = []
        tracemalloc.start()
        for _ in range(5):
            start = time.perf_counter_ns()
            predict(value)
            timing.append((time.perf_counter_ns() - start) / 1e6)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rows.append(
            {
                "source": "SYNTHETIC_DENSE_LOAD_FROM_PRODUCTION_FIXTURE",
                "history_count": size,
                "median_ms": statistics.median(timing),
                "p95_ms": max(timing),
                "repetitions": 5,
                "tracemalloc_peak_bytes": peak,
                "input_bytes": len(json.dumps(value).encode()),
            }
        )
    execution = []
    for enabled in (False, True):
        elapsed = []
        drained = []
        bytes_written = []
        for index in range(5):
            a = tool_arguments()
            box = make_toolbox(
                ROOT / "performance-data" / f"graph-{enabled}-{index}",
                enabled,
                [approve("seed", a), approve("repeat", a)],
            )
            start = time.perf_counter_ns()
            await graph_run(box, [[("http_request", a, "seed")], [("http_request", a, "repeat")]])
            elapsed.append((time.perf_counter_ns() - start) / 1e6)
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            drained.append((time.perf_counter_ns() - start) / 1e6)
            bytes_written.append(
                sum(
                    p.stat().st_size
                    for p in (box.settings.data_root / "history_action_shadow").rglob("*.json")
                )
                if enabled
                else 0
            )
            await box.executor.close()
        execution.append(
            {
                "shadow_enabled": enabled,
                "repetitions": 5,
                "agent_return_p50_ms": statistics.median(elapsed),
                "agent_return_p95_ms": max(elapsed),
                "including_drain_p50_ms": statistics.median(drained),
                "shadow_persisted_bytes_per_two_calls": bytes_written,
            }
        )
    output = {
        "dense_history": rows,
        "off_on_execution": execution,
        "limits": "local fixture + tracemalloc; small repeated sample; no live performance or savings claim",
    }
    (ROOT / "results/performance.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
