"""预声明三臂开发负载；仅生产合同 + 内存 SQL + MockTransport，不是盲评。"""

from __future__ import annotations

import asyncio
import cProfile
import importlib.util
import json
import math
import statistics
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, cast

from experiments.history_action_autonomous_shadow_v1.fixture import make_box
from experiments.history_action_trusted_shadow_v1.fixture import business, graph_run

from agent_workbench.domain.history_action_reflection import digest
from agent_workbench.tools import history_action_shadow as active
from agent_workbench.tools.shadow_test_profile import run_http_test_profile

ROOT = Path(__file__).resolve().parent


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def original_service():
    def module(name, relative) -> Any:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "baseline/src/agent_workbench" / relative
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("baseline_module_unavailable")
        result = importlib.util.module_from_spec(spec)
        sys.modules[name] = result
        spec.loader.exec_module(result)
        return result

    store = module("stage4_store", "tools/shadow_storage.py")
    domain = module("stage4_domain", "domain/trusted_http_shadow.py")
    service = module("stage4_service", "tools/history_action_shadow.py")
    service.ShadowStore = store.ShadowStore
    service.adapt_history = domain.adapt_history
    return service.ShadowService


class Meter:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.measured: defaultdict[str, float] = defaultdict(float)

    def enqueue(self, *args, **kwargs):
        start, cpu = time.perf_counter(), time.thread_time()
        try:
            return cast(Any, super()).enqueue(*args, **kwargs)
        finally:
            self.measured["enqueue_wall_ms"] += (time.perf_counter() - start) * 1000
            self.measured["enqueue_thread_cpu_ms"] += (time.thread_time() - cpu) * 1000

    def _write(self, *args, **kwargs):
        start, cpu = time.perf_counter(), time.thread_time()
        try:
            return cast(Any, super())._write(*args, **kwargs)
        finally:
            self.measured["write_wall_ms"] += (time.perf_counter() - start) * 1000
            self.measured["write_thread_cpu_ms"] += (time.thread_time() - cpu) * 1000
            self.measured["writes"] += 1

    def _compare(self, *args, **kwargs):
        start, cpu = time.perf_counter(), time.thread_time()
        try:
            return cast(Any, super())._compare(*args, **kwargs)
        finally:
            self.measured["compare_wall_ms"] += (time.perf_counter() - start) * 1000
            self.measured["compare_thread_cpu_ms"] += (time.thread_time() - cpu) * 1000


async def trial(arm, count, name, *, traced=True):
    implementation = original_service() if arm == "STAGE4" else OPTIMIZED
    measured = type("MeasuredShadow", (Meter, implementation), {})
    previous = active.ShadowService
    active.ShadowService = measured
    box = make_box(ROOT / "performance-data" / name, arm != "OFF")
    lag = []
    finished = False

    async def heartbeat():
        deadline = time.perf_counter() + 0.005
        while not finished:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            lag.append(max(0, now - deadline) * 1000)
            deadline = now + 0.005

    pulse = asyncio.create_task(heartbeat())
    if traced:
        tracemalloc.start()
    start, cpu = time.perf_counter(), time.process_time()
    try:
        rounds = [
            [("run_http_test_profile", {"profile_id": "baseline"}, f"call-{n}")]
            for n in range(count)
        ]
        graph, model, result, config, _ = await graph_run(
            box, rounds, tool_impl=run_http_test_profile
        )
        business_end = time.perf_counter()
        service = box.history_action_shadow
        if service:
            await service.drain()
        end, process_cpu = time.perf_counter(), time.process_time() - cpu
        peak = tracemalloc.get_traced_memory()[1] if traced else None
        if traced:
            tracemalloc.stop()
        snapshot = await graph.aget_state(config)
        view = business(box, result, model)
        # Result is checkpoint values; compare full normalized messages and business state.
        assert snapshot.values == result
        assert len(box.executor.jobs) == count
        files = list((box.settings.data_root / "history_action_shadow").rglob("*.json"))
        timing = Counter()
        outcomes = service.outcomes if service else []
        for outcome in outcomes:
            timing.update(outcome.get("timing_ms", {}))
        return {
            "arm": arm,
            "calls": count,
            "name": name,
            "total_ms": (end - start) * 1000,
            "graph_ms": (business_end - start) * 1000,
            "drain_ms": (end - business_end) * 1000,
            "process_cpu_ms": process_cpu * 1000,
            "peak_bytes": peak,
            "shadow_bytes": sum(p.stat().st_size for p in files),
            "shadow_files": len(files),
            "skipped": service.skipped if service else 0,
            "outcomes": len(outcomes),
            "triggers": sum(o.get("emit_shadow_event", False) for o in outcomes),
            "duplicates": sum(o.get("duplicate_suppressed", False) for o in outcomes),
            "classifications": dict(Counter(o["classification"] for o in outcomes)),
            "timings_ms_sum_over_observations": dict(timing),
            "measured": dict(service.measured) if service else {},
            "event_loop_lag_max_ms": max(lag, default=0),
            "business_sha256": digest(view),
            "model_requests": len(model.inputs),
        }
    finally:
        if traced and tracemalloc.is_tracing():
            tracemalloc.stop()
        finished = True
        await pulse
        if box.history_action_shadow:
            await box.history_action_shadow.aclose()
        await box.executor.close()
        active.ShadowService = previous


OPTIMIZED = active.ShadowService


async def main(profile=False):
    if profile:
        profiler = cProfile.Profile()
        profiler.enable()
        row = await trial("STAGE4", 16, "profile-stage4", traced=False)
        profiler.disable()
        profiler.dump_stats(str(ROOT / "stage4-profile.pstats"))
        write(ROOT / "stage4-profile.json", row)
        return
    if (ROOT / "performance.json").exists():
        raise RuntimeError("performance_already_run")
    for arm in ("OFF", "STAGE4", "OPTIMIZED"):
        await trial(arm, 2, "warmup-" + arm, traced=False)
    rows = []
    for count in (2, 16, 64):
        for repetition in range(5):
            arms = ["OFF", "STAGE4", "OPTIMIZED"]
            arms = arms[repetition % 3 :] + arms[: repetition % 3]
            for arm in arms:
                row = await trial(arm, count, f"{count}-{repetition}-{arm}")
                rows.append(row)
                print(
                    json.dumps({k: row[k] for k in ("arm", "calls", "total_ms", "skipped")}),
                    flush=True,
                )
    aggregates = []
    for count in (2, 16, 64):
        for arm in ("OFF", "STAGE4", "OPTIMIZED"):
            selected = [r for r in rows if r["calls"] == count and r["arm"] == arm]
            times = sorted(r["total_ms"] for r in selected)
            aggregates.append(
                {
                    "arm": arm,
                    "calls": count,
                    "p50_ms": statistics.median(times),
                    "p95_ms": times[math.ceil(0.95 * len(times)) - 1],
                    "cpu_p50_ms": statistics.median(r["process_cpu_ms"] for r in selected),
                    "peak_max_bytes": max(r["peak_bytes"] for r in selected),
                    "storage_p50_bytes": statistics.median(r["shadow_bytes"] for r in selected),
                    "business_hashes": sorted({r["business_sha256"] for r in selected}),
                    "skipped": [r["skipped"] for r in selected],
                    "triggers": [r["triggers"] for r in selected],
                }
            )
    write(
        ROOT / "performance.json",
        {
            "rows": rows,
            "aggregates": aggregates,
            "protocol": "protocol.json; memory SQL, NOT measured PostgreSQL time; traced development load",
        },
    )


if __name__ == "__main__":
    asyncio.run(main("--profile" in sys.argv))
