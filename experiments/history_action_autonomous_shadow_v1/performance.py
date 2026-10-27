"""开发负载基准；内存 SQL 替身与 MockTransport，不是新的准确率评价。"""

from __future__ import annotations

import asyncio
import statistics
import time
import tracemalloc

from experiments.history_action_autonomous_shadow_v1.evaluate import ROOT, write
from experiments.history_action_autonomous_shadow_v1.fixture import make_box
from experiments.history_action_trusted_shadow_v1.fixture import graph_run

from agent_workbench.tools.shadow_test_profile import run_http_test_profile


async def main():
    rows = []
    for count in (2, 16, 64):
        for enabled in (False, True):
            elapsed, peaks, storage, skipped = [], [], [], []
            for trial in range(3):
                box = make_box(ROOT / "performance-data" / f"{count}-{enabled}-{trial}", enabled)
                rounds = [
                    [("run_http_test_profile", {"profile_id": "baseline"}, f"call-{n}")]
                    for n in range(count)
                ]
                tracemalloc.start()
                start = time.perf_counter()
                try:
                    await graph_run(box, rounds, tool_impl=run_http_test_profile)
                    if box.history_action_shadow:
                        await box.history_action_shadow.drain()
                    elapsed.append((time.perf_counter() - start) * 1000)
                    peaks.append(tracemalloc.get_traced_memory()[1])
                    shadow_root = box.settings.data_root / "history_action_shadow"
                    storage.append(sum(p.stat().st_size for p in shadow_root.rglob("*.json")))
                    skipped.append(
                        box.history_action_shadow.skipped if box.history_action_shadow else 0
                    )
                    assert len(box.executor.jobs) == count
                finally:
                    tracemalloc.stop()
                    if box.history_action_shadow:
                        await box.history_action_shadow.aclose()
                    await box.executor.close()
            rows.append(
                {
                    "source": "PRODUCTION_CONTRACT_MEMORY_SQL_MOCK_EXECUTOR_DEVELOPMENT_LOAD",
                    "total_tool_calls": count,
                    "shadow_enabled": enabled,
                    "repetitions": 3,
                    "p50_ms": statistics.median(elapsed),
                    "p95_ms_nearest_rank": max(elapsed),
                    "peak_bytes": max(peaks),
                    "shadow_bytes": storage,
                    "enqueue_skipped": skipped,
                }
            )
    write(
        ROOT / "performance-results.json",
        {
            "rows": rows,
            "limits": "3 repetitions, tracemalloc, whole graph/context/checkpoint included; not real production throughput; large histories may abstain under default budgets",
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
