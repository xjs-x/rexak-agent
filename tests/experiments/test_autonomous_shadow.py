"""自主上下文和资源保护开发回归；不作为盲评或真实目标效果证据。"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from experiments.history_action_autonomous_shadow_v1.fixture import (
    make_box,
    policy_for,
    save_policy,
)
from experiments.history_action_trusted_shadow_v1.fixture import business, graph_run, tool_arguments

from agent_workbench.domain.shadow_authorization import load_authorization
from agent_workbench.tools import history_action_shadow as shadow
from agent_workbench.tools.http import http_request
from agent_workbench.tools.registry import build_tools
from agent_workbench.tools.shadow_storage import ShadowStorageBusy, ShadowStorageLimit, ShadowStore
from agent_workbench.tools.shadow_test_profile import run_http_test_profile


async def close(box):
    if box.history_action_shadow:
        await box.history_action_shadow.aclose()
    await box.executor.close()


def test_prototype_is_not_registered_and_http_schema_is_unchanged():
    assert "run_http_test_profile" not in {tool.name for tool in build_tools()}
    assert set(cast(Any, run_http_test_profile.tool_call_schema).model_fields) == {"profile_id"}
    assert "trusted" not in cast(Any, http_request.tool_call_schema).model_fields


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "revoked"),
        ("run_id", "another-run"),
        ("session_id", "another-session"),
        ("revision", 0),
        ("scope_revision", 99),
        ("scope_sha256", "bad"),
        ("policy_revision", 2),
        ("control_version", 1),
        ("executor_boot_id", "another-boot"),
        ("http_enabled", False),
        ("valid_until", (datetime.now(UTC) - timedelta(days=1)).isoformat()),
    ],
)
async def test_task_authorization_cannot_outlive_its_control_boundary(tmp_path, field, value):
    box = make_box(tmp_path)
    policy = policy_for(box)
    policy[field] = value
    save_policy(box, policy)
    try:
        with pytest.raises(ValueError):
            load_authorization(box)
    finally:
        await close(box)


async def test_recreated_service_deduplicates_with_task_context_not_per_call_grants(tmp_path):
    from experiments.history_action_trusted_shadow_v1.fixture import parsed

    from agent_workbench.domain.shadow_authorization import bind_profile

    box = make_box(tmp_path)
    try:
        _, _, result, _, runtime_context = await graph_run(
            box,
            [
                [("run_http_test_profile", {"profile_id": "baseline"}, key)]
                for key in ["seed", "repeat"]
            ],
            tool_impl=run_http_test_profile,
        )
        await box.history_action_shadow.aclose()
        box.history_action_shadow = shadow.ShadowService(box)
        arguments = parsed(tool_arguments()).model_dump(exclude={"note"})
        view = shadow.dispatch_context(
            type("Runtime", (), {"state": result, "context": runtime_context})()
        )
        view["messages"] = view["messages"][:4]
        view["profile_binding"] = bind_profile(load_authorization(box), "baseline", arguments)
        box.history_action_shadow.enqueue(arguments, box.executor.submissions[-1], "repeat", view)
        await box.history_action_shadow.drain()
        assert box.history_action_shadow.outcomes[0]["duplicate_suppressed"]
        assert len(list((box.history_action_shadow.root / "events").glob("*.json"))) == 1
        assert len(box.executor.jobs) == 2
    finally:
        await close(box)


async def test_controlled_recipe_cannot_expand_original_scope(tmp_path):
    from experiments.history_action_trusted_shadow_v1.fixture import parsed

    box = make_box(
        tmp_path,
        profiles={
            "baseline": {
                "request": parsed(tool_arguments(url="https://outside.invalid/")).model_dump(
                    exclude={"note"}
                )
            }
        },
    )
    try:
        await graph_run(
            box,
            [[("run_http_test_profile", {"profile_id": "baseline"}, "attempt")]],
            tool_impl=run_http_test_profile,
        )
        assert not box.executor.submissions
        assert box.history_action_shadow is None
    finally:
        await close(box)


async def test_model_dictionary_cannot_construct_controller_binding(tmp_path):
    from agent_workbench.domain.shadow_authorization import grant_for_binding

    box = make_box(tmp_path)
    try:
        with pytest.raises(ValueError, match="untrusted_profile_binding"):
            grant_for_binding(box, {"trusted": True, "profile_id": "baseline"}, {}, {}, "fake")
    finally:
        await close(box)


async def test_parent_cancel_during_observer_close_is_not_swallowed(tmp_path):
    box = make_box(tmp_path)
    service = shadow.ShadowService(box)
    sleeper = asyncio.create_task(asyncio.sleep(1))
    service.pending.add(sleeper)
    try:
        task = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sleeper.cancelled() and service._closed
    finally:
        await close(box)


async def test_large_dispatch_input_is_skipped_before_queue_copy(tmp_path):
    from experiments.history_action_trusted_shadow_v1.fixture import parsed

    request = parsed(tool_arguments(method="POST", body="x" * 300000)).model_dump(exclude={"note"})
    box = make_box(tmp_path, profiles={"baseline": {"request": request}})
    try:
        await graph_run(
            box,
            [[("run_http_test_profile", {"profile_id": "baseline"}, "large")]],
            tool_impl=run_http_test_profile,
        )
        assert len(box.executor.jobs) == 1
        assert box.history_action_shadow.skipped == 1
        assert not box.history_action_shadow.pending
    finally:
        await close(box)


async def test_storage_cannot_escape_its_root(tmp_path):
    box = make_box(tmp_path)
    store = ShadowStore(tmp_path / "observations", box.settings)
    try:
        with pytest.raises(ValueError, match="boundary"):
            store.write(store.root / ".." / "other.json", {"data": "bounded"})
        assert not (tmp_path / "other.json").exists()
    finally:
        await close(box)


async def test_close_deadline_cancels_unfinished_observation(tmp_path):
    box = make_box(tmp_path)
    box.settings.history_action_shadow_close_seconds = 0.01
    service = shadow.ShadowService(box)
    pending = asyncio.create_task(asyncio.Event().wait())
    service.pending.add(pending)
    started = time.perf_counter()
    try:
        await service.aclose()
        assert pending.cancelled() and service._closed
        assert time.perf_counter() - started < 0.2
    finally:
        await close(box)


def test_cross_process_store_claim_is_at_most_once(tmp_path):
    import subprocess
    import sys

    script = """from pathlib import Path
from types import SimpleNamespace
import sys
from agent_workbench.tools.shadow_storage import ShadowStore, ShadowStorageBusy
s=SimpleNamespace(history_action_shadow_record_bytes=65536,history_action_shadow_storage_files=512,history_action_shadow_storage_bytes=8388608)
store=ShadowStore(Path(sys.argv[1]),s)
count=0
for _ in range(16):
 try: count+=store.write(store.root/'event.json',{'key':'stable'})
 except ShadowStorageBusy: pass
print(count)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path / "shared")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outputs = [child.communicate(timeout=30) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert sum(int(stdout) for stdout, _ in outputs) == 1


async def test_no_per_call_approval_is_needed_for_new_ids(tmp_path):
    box = make_box(tmp_path)
    before = box.settings.history_action_shadow_context_path.read_bytes()
    try:
        await graph_run(
            box,
            [
                [("run_http_test_profile", {"profile_id": "baseline"}, name)]
                for name in ["seed", "r1", "r2", "r3"]
            ],
            tool_impl=run_http_test_profile,
        )
        await box.history_action_shadow.drain()
        assert len(box.executor.jobs) == 4
        assert (
            sum(o.get("emit_shadow_event", False) for o in box.history_action_shadow.outcomes) == 1
        )
        assert box.settings.history_action_shadow_context_path.read_bytes() == before
        assert box.settings.history_action_shadow_grants_path is None
    finally:
        await close(box)


async def test_navigation_does_not_inherit_task_security_semantics(tmp_path):
    box = make_box(tmp_path)
    try:
        await graph_run(
            box, [[("http_request", tool_arguments(), name)] for name in ["one", "two"]]
        )
        await box.history_action_shadow.drain()
        assert len(box.executor.jobs) == 2
        assert all(
            o["classification"] == "NOT_IN_SCOPE" for o in box.history_action_shadow.outcomes
        )
    finally:
        await close(box)


async def test_overload_drops_observations_without_blocking_original_tools(tmp_path):
    projections = []
    for enabled in (False, True):
        box = make_box(tmp_path / str(enabled), enabled)
        box.settings.history_action_shadow_queue_limit = 1
        service = None
        if enabled:
            service = shadow.ShadowService(box)
            box.history_action_shadow = service
            await service._gate.acquire()
        try:
            _, model, result, _, _ = await graph_run(
                box,
                [
                    [
                        ("run_http_test_profile", {"profile_id": "baseline"}, f"c{i}")
                        for i in range(12)
                    ]
                ],
                tool_impl=run_http_test_profile,
            )
            if enabled:
                assert service is not None
                assert len(service.pending) <= 1 and service.skipped >= 11
                service._gate.release()
                await service.drain()
            assert len(box.executor.jobs) == 12
            projections.append(business(box, result, model))
        finally:
            await close(box)
    assert projections[0] == projections[1]


@pytest.mark.parametrize("limit", ["record", "count", "bytes", "busy"])
async def test_bounded_storage_never_deletes_existing_files(tmp_path, limit):
    box = make_box(tmp_path)
    store = ShadowStore(tmp_path / "store", box.settings)
    try:
        sentinel = store.root / "sentinel.json"
        store.root.mkdir(exist_ok=True)
        sentinel.write_text('"preserve"', encoding="utf-8")
        before = sentinel.read_bytes()
        if limit == "record":
            box.settings.history_action_shadow_record_bytes = 10
        elif limit == "count":
            box.settings.history_action_shadow_storage_files = 1
        elif limit == "bytes":
            box.settings.history_action_shadow_storage_bytes = 10
        else:
            (store.root / ".writer.lock").write_text("belongs to another writer", encoding="utf-8")
        with pytest.raises((ShadowStorageLimit, ShadowStorageBusy)):
            store.write(store.root / "new.json", {"content": "x" * 32})
        assert sentinel.read_bytes() == before
        assert not (store.root / "new.json").exists()
    finally:
        await close(box)


async def test_atomic_claim_across_competing_stores(tmp_path):
    box = make_box(tmp_path)
    store = ShadowStore(tmp_path / "claims", box.settings)

    def attempt(_):
        try:
            return store.write(store.root / "event.json", {"key": "same"})
        except ShadowStorageBusy:
            return False

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert sum(pool.map(attempt, range(32))) == 1
    finally:
        await close(box)


async def test_cpu_work_is_off_event_loop_and_close_is_bounded(tmp_path, monkeypatch):
    box = make_box(tmp_path)
    box.settings.history_action_shadow_close_seconds = 0.01
    service = shadow.ShadowService(box)
    original = service._compare

    def slow(*args):
        time.sleep(0.1)
        return original(*args)

    monkeypatch.setattr(service, "_compare", slow)
    box.history_action_shadow = service
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            ticks += 1
            await asyncio.sleep(0.001)

    try:
        await asyncio.gather(
            graph_run(
                box,
                [[("run_http_test_profile", {"profile_id": "baseline"}, "seed")]],
                tool_impl=run_http_test_profile,
            ),
            ticker(),
        )
        started = time.perf_counter()
        await service.aclose()
        assert time.perf_counter() - started < 0.15
        assert ticks == 20 and not service.pending
        assert service._closed
    finally:
        await close(box)
