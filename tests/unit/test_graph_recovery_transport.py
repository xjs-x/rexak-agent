"""No-provider tests of experiment deadlines, cleanup and durable accounting."""

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

PATH = (
    Path(__file__).resolve().parents[2]
    / "experiments/graph_error_recovery_ab_20261028/reliable_transport.py"
)
spec = importlib.util.spec_from_file_location("graph_recovery_reliable", PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SCHEMA = [{"type": "function", "function": {"name": n}} for n in ["get_graph", "update_graph"]]


def make(tmp_path, handler, **kw):
    t = mod.ReliableTransport(
        tmp_path / "ledger.jsonl",
        "https://test.invalid/v1",
        {"recovery": {"A": SCHEMA}},
        delegate=httpx.MockTransport(handler),
        wall_seconds=0.03,
        grace_seconds=0.03,
        **kw,
    )
    t.active = ("recovery", "case", "A")
    return t


def request():
    return httpx.Request(
        "POST",
        "https://test.invalid/v1/chat/completions",
        json={
            "tools": SCHEMA,
            "model": "deepseek-v4.1-flash",
            "temperature": 1,
            "max_tokens": 4096,
        },
    )


async def test_charge_is_fsynced_before_transport_and_response_settled(tmp_path):
    async def handler(req):
        assert (
            json.loads((tmp_path / "ledger.jsonl").read_text("utf-8").splitlines()[-1])["stage"]
            == "submitted"
        )
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "choices": [{"finish_reason": "stop"}],
            },
        )

    t = make(tmp_path, handler)
    await t.handle_async_request(request())
    assert t.totals() == (5, 3)
    await t.aclose()


async def test_never_returning_transport_is_cancelled_and_not_replayed(tmp_path):
    cancelled = []

    async def handler(req):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    t = make(tmp_path, handler)
    with pytest.raises(TimeoutError):
        await t.handle_async_request(request())
    assert cancelled and not t.tasks
    with pytest.raises(mod.ExperimentStopped):
        await t.handle_async_request(request())
    rows = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text("utf-8").splitlines()]
    assert sum(r["stage"] == "submitted" for r in rows) == 1
    assert rows[-1]["provider_settlement"] == "UNKNOWN"
    assert rows[-1]["cancellation"] == "reclaimed"


async def test_cancellation_resistant_task_has_bounded_grace(tmp_path):
    release = asyncio.Event()

    async def handler(req):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return httpx.Response(200, json={})

    t = make(tmp_path, handler)
    with pytest.raises(TimeoutError):
        await t.handle_async_request(request())
    assert t.tasks
    assert (
        json.loads((tmp_path / "ledger.jsonl").read_text("utf-8").splitlines()[-1])["cancellation"]
        == "uncertain"
    )
    release.set()
    await asyncio.gather(*t.tasks)


async def test_external_cancel_propagates_and_remains_unknown(tmp_path):
    async def handler(req):
        await asyncio.Event().wait()

    t = make(tmp_path, handler)
    task = asyncio.create_task(t.handle_async_request(request()))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not t.tasks and t.stopped


async def test_no_budget_no_submission_and_restart_cannot_clear_ledger(tmp_path):
    async def handler(req):
        raise AssertionError("must not send")

    t = make(tmp_path, handler, max_requests=0)
    with pytest.raises(mod.ExperimentStopped):
        await t.handle_async_request(request())
    assert not (tmp_path / "ledger.jsonl").exists()
    (tmp_path / "ledger.jsonl").write_text('{"stage":"submitted"}\n', encoding="utf-8")
    with pytest.raises(mod.ExperimentStopped):
        make(tmp_path, handler)


async def test_unknown_usage_is_charged_and_stops(tmp_path):
    async def handler(req):
        return httpx.Response(200, json={"choices": []})

    t = make(tmp_path, handler)
    with pytest.raises(mod.ExperimentStopped):
        await t.handle_async_request(request())
    assert t.stopped and not t.receipts
    assert "UNKNOWN" in (tmp_path / "ledger.jsonl").read_text("utf-8")


async def test_sdk_explicit_timeout_reaches_wire_and_old_default_clears_it():
    from langchain_openai import ChatOpenAI

    observed = []

    async def handler(req):
        observed.append(req.extensions["timeout"])
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    for explicit in (False, True):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=300) as client:
            config: dict[str, Any] = (
                {"timeout": httpx.Timeout(60, connect=15, read=15, write=15, pool=15)}
                if explicit
                else {}
            )
            model = ChatOpenAI(
                model="synthetic",
                api_key=SecretStr("synthetic"),
                base_url="https://test.invalid/v1",
                http_async_client=client,
                max_retries=0,
                **config,
            )
            await model.ainvoke("synthetic")
    assert observed[0] == dict.fromkeys(["connect", "read", "write", "pool"])
    assert observed[1] == dict.fromkeys(["connect", "read", "write", "pool"], 15)


def test_process_watchdog_stops_only_owned_child(tmp_path, monkeypatch):
    import subprocess
    import sys
    from datetime import UTC, datetime, timedelta

    directory = PATH.parent
    monkeypatch.syspath_prepend(str(directory))
    experiment_spec = importlib.util.spec_from_file_location(
        "graph_recovery_runner_test", directory / "run_recovery.py"
    )
    assert experiment_spec and experiment_spec.loader
    runner = importlib.util.module_from_spec(experiment_spec)
    experiment_spec.loader.exec_module(runner)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    mod.append(
        tmp_path / "physical-ledger.jsonl",
        {
            "stage": "submitted",
            "physical_request": 1,
            "utc": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
            "deadline_seconds": 0.01,
            "cancellation_grace_seconds": 0.01,
        },
    )
    try:
        assert runner.watch_process(child, tmp_path) == 72
        assert child.poll() is not None
        assert "uncertain" in (tmp_path / "watchdog.jsonl").read_text("utf-8")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("limit", ["input_limit", "output_limit"])
async def test_token_reservations_stop_before_send(tmp_path, limit):
    async def handler(req):
        raise AssertionError("must not send")

    transport = make(tmp_path, handler, **{limit: 1})
    with pytest.raises(mod.ExperimentStopped):
        await transport.handle_async_request(request())
    assert not transport.ledger.exists()


def test_frozen_experiments_change_only_intended_schema_variable():
    plan = json.loads((PATH.parent / "freeze.json").read_text("utf-8"))
    assert len(plan["cases"]) == 14 and len(plan["order"]) == 56
    assert plan["schemas"]["recovery"]["A"] == plan["schemas"]["recovery"]["B"]
    assert plan["schemas"]["construction"]["A"] != plan["schemas"]["construction"]["B"]
    assert sum(c["oracle_operations"] is not None for c in plan["cases"]) == 11
    for case in plan["cases"]:
        assert "oracle_operations" not in case["visible"]
        assert "accepted_repair" not in case["visible"]
        assert case["fixture"]["failed_call"]["tool_call_id"]
