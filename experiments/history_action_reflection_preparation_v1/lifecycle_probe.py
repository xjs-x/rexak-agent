"""补充生命周期诊断：每臂每负载一次，不替换预声明五重复性能结果。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from experiments.history_action_reflection_preparation_v1 import benchmark


async def main():
    rows = []
    original = benchmark.original_service
    optimized = benchmark.OPTIMIZED
    closed = []

    class Closure:
        async def aclose(self):
            start, cpu = time.perf_counter(), time.process_time()
            try:
                await cast(Any, super()).aclose()
            finally:
                work = getattr(self, "_computation", None)
                closed.append({"close_wall_ms": (time.perf_counter() - start) * 1000,
                               "close_process_cpu_ms": (time.process_time() - cpu) * 1000,
                               "worker_still_running": work is not None and not work.done()})

    benchmark.original_service = lambda: type("OldClosure", (Closure, original()), {})
    benchmark.OPTIMIZED = type("NewClosure", (Closure, optimized), {})
    try:
        for count in (2, 16, 64):
            for arm in ("OFF", "STAGE4", "OPTIMIZED"):
                before = len(closed)
                start, cpu = time.perf_counter(), time.process_time()
                row = await benchmark.trial(arm, count, f"lifecycle-{count}-{arm}", traced=False)
                rows.append({"arm": arm, "calls": count, "whole_harness_ms": (time.perf_counter() - start) * 1000,
                             "whole_harness_cpu_ms": (time.process_time() - cpu) * 1000,
                             "graph_and_drain_ms": row["total_ms"],
                             "closure": closed[before:], "skipped": row["skipped"]})
        benchmark.write(benchmark.ROOT / "lifecycle-diagnostic.json", {"repetitions": 1, "tracemalloc": False,
                         "role": "ADDITIONAL_DIAGNOSTIC_NOT_REPLACEMENT_OR_TAIL_ESTIMATE", "rows": rows})
    finally:
        benchmark.original_service, benchmark.OPTIMIZED = original, optimized


if __name__ == "__main__":
    asyncio.run(main())
