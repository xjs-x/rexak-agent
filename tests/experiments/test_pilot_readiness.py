"""Pilot engineering protocol, never real provider or target tests."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import httpx
import openai
import pytest
from experiments.history_action_reflection_pilot_readiness_v1.budget import (
    AsyncGuardTransport,
    BudgetStop,
    GuardTransport,
    Ledger,
    request_scope,
)
from experiments.history_action_reflection_pilot_readiness_v1.evaluate import summarize
from experiments.history_action_reflection_pilot_readiness_v1.protocol import ROOT, review_check
from experiments.history_action_reflection_pilot_readiness_v1.provider_preflight import build
from experiments.history_action_reflection_pilot_readiness_v1.rehearsal import (
    PassiveToolReference,
    close,
    equivalence,
    make_isolated,
    mock_response,
    run_one,
    seed,
)
from experiments.history_action_reflection_preparation_v1.reflection import FeedbackLedger
from langchain_core.messages import HumanMessage

from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.tools.evidence import read_artifact


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect
    socketpair_code = socket.socketpair.__code__

    def deny(*args, **kwargs):
        raise AssertionError("NO_NETWORK_IN_READINESS_TESTS")

    def guarded_connect(sock, address):
        # Windows asyncio creates its self-pipe with Python's fallback socketpair.
        if sys._getframe(1).f_code is socketpair_code and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("NO_NETWORK_IN_READINESS_TESTS")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def payload():
    return {
        "model": "deepseek-v4.1-flash",
        "stream": False,
        "max_completion_tokens": 16384,
        "messages": [{"role": "user", "content": "offline"}],
    }


def send(ledger, key="one", *, run="r", kind="ordinary", handler=mock_response):
    with (
        request_scope(run, key, kind),
        httpx.Client(transport=GuardTransport(ledger, httpx.MockTransport(handler))) as client,
    ):
        return client.post("https://provider.invalid/v1/chat/completions", json=payload())


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("normal", "complete"),
        ("error", "provider_error"),
        ("429", "provider_error"),
        ("timeout", "unknown_response"),
        ("lost", "unknown_response"),
        ("missing_usage", "usage_missing"),
        ("invalid", "invalid_response"),
        ("invalid_usage", "usage_missing"),
        ("interrupt", "unknown_response"),
    ],
)
async def test_production_adapter_physical_faults(tmp_path, fault, expected):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")
    seen = []

    def handler(request):
        # This observation proves durable reserve happened BEFORE the actual transport.
        rows = Ledger(tmp_path / "ledger.sqlite").rows()
        assert len(rows) == 1 and rows[0]["state"] == "possibly_sent"
        wire = json.loads(request.content)
        assert wire["temperature"] == 1
        assert wire["reasoning_effort"] == "high"
        assert wire["max_completion_tokens"] == 16384 and not wire["stream"]
        assert "max_tokens" not in wire
        seen.append(wire)
        if fault == "error":
            return httpx.Response(500, json={"error": {"message": "offline error"}})
        if fault == "429":
            return httpx.Response(429, json={"error": {"message": "offline rate limit"}})
        if fault == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if fault == "lost":
            raise httpx.ReadError("sent, response lost", request=request)
        if fault == "interrupt":
            raise asyncio.CancelledError()
        response = mock_response(request)
        value = response.json()
        if fault == "missing_usage":
            value.pop("usage")
        if fault == "invalid_usage":
            value["usage"]["total_tokens"] = 0
        if fault == "invalid":
            value["choices"] = []
        return httpx.Response(200, json=value)

    model, binding, sync, asynchronous = build(ledger, handler)
    try:
        assert binding.profile.max_response_recoveries == 0
        assert model.max_retries == 0
        with request_scope("r", "logical-1"):
            if fault == "normal":
                reply = await model.ainvoke([HumanMessage(content="offline")])
                assert reply.content == "mock"
            else:
                # Locked LangChain currently mishandles CancelledError after gather.
                expected_errors = (
                    (AttributeError,) if fault == "interrupt" else (openai.APIError, BudgetStop)
                )
                with pytest.raises(expected_errors):
                    await model.ainvoke([HumanMessage(content="offline")])
        assert len(seen) == 1
        assert ledger.rows()[0]["state"] == expected
        with request_scope("r", "logical-1"), pytest.raises((openai.APIError, BudgetStop)):
            await model.ainvoke([HumanMessage(content="replay must fail")])
        assert len(seen) == 1
        if fault != "normal":
            with pytest.raises(BudgetStop, match="unresolved_or_failed"):
                send(ledger, "new-id-cannot-reset-unknown")
    finally:
        sync.close()
        await asynchronous.aclose()


async def test_sdk_forced_retry_attempt_is_blocked_before_second_send(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    model, _, sync, asynchronous = build(ledger, handler)
    model.async_client = model.root_async_client.with_options(max_retries=1).chat.completions
    try:
        with request_scope("r", "same-id"), pytest.raises((openai.APIError, BudgetStop)):
            await model.ainvoke([HumanMessage(content="offline retry injection")])
        assert len(sent) == 1 and len(ledger.rows()) == 1
    finally:
        sync.close()
        await asynchronous.aclose()


async def test_sync_sdk_path_is_guarded_too(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")
    model, _, sync, asynchronous = build(ledger, mock_response)
    try:
        with request_scope("r", "sync"):
            assert model.invoke([HumanMessage(content="offline")]).content == "mock"
        assert len(ledger.rows()) == 1
    finally:
        sync.close()
        await asynchronous.aclose()


async def test_actual_sdk_serializes_tool_reference_and_production_schemas(tmp_path):
    from experiments.history_action_reflection_pilot_readiness_v1.rehearsal import TOOLS
    from langchain_core.messages import messages_from_dict

    trace = json.loads((ROOT / "results/runs/A-1-C/trace.json").read_text(encoding="utf-8"))
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return mock_response(request)

    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")
    model, _, sync, asynchronous = build(ledger, handler)
    view = next(
        v
        for v in trace["model_inputs"]
        if any("_host_history_reference" in str(m.get("content")) for m in v)
    )
    messages = messages_from_dict([{"type": m["type"], "data": m} for m in view])
    try:
        with request_scope("r", "notice-wire"):
            await model.bind_tools(TOOLS).ainvoke(messages)
        assert len(captured) == 1
        packet = captured[0]
        assert {t["function"]["name"] for t in packet["tools"]} == {t.name for t in TOOLS}
        notes = [
            m for m in packet["messages"] if "_host_history_reference" in str(m.get("content"))
        ]
        assert len(notes) == 1 and notes[0]["role"] == "tool"
        assert notes[0]["tool_call_id"] == trace["deliveries"][0]["tool_call_id"]
        assert len([m for m in packet["messages"] if m["role"] == "user"]) == 2
    finally:
        sync.close()
        await asynchronous.aclose()


def test_run_counts_and_maintenance_share_global_budget(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", total_cap=9)
    for run in ("r", "s"):
        ledger.register(run)
    for i in range(6):
        send(ledger, str(i))
    with pytest.raises(BudgetStop, match="run_physical"):
        send(ledger, "overflow")
    for i in range(2):
        send(ledger, "m" + str(i), kind="maintenance")
    with pytest.raises(BudgetStop, match="run_physical"):
        send(ledger, "m-overflow", kind="maintenance")
    send(ledger, "last", run="s")
    with pytest.raises(BudgetStop, match="pilot_physical"):
        send(ledger, "beyond", run="s")
    assert len(ledger.rows()) == 9


def test_native_usage_missing_and_remaining_token_gate(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")

    def large(request):
        value = mock_response(request).json()
        value["usage"] = {"prompt_tokens": 250000, "completion_tokens": 100, "total_tokens": 250100}
        return httpx.Response(200, json=value)

    send(ledger, handler=large)
    with pytest.raises(BudgetStop, match="native_token_remaining"):
        send(ledger, "next")
    assert len(ledger.rows()) == 1


def test_process_crash_reserve_survives_and_cannot_replay(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path)
    ledger.register("r")
    code = """
import sys, os, httpx
from pathlib import Path
from experiments.history_action_reflection_pilot_readiness_v1.budget import Ledger,request_scope
with request_scope('r','crashed'):
    Ledger(Path(sys.argv[1])).reserve(httpx.Request('POST','https://provider.invalid/v1/chat/completions',json={'stream':False,'max_completion_tokens':16384}))
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], check=False, timeout=20)
    assert result.returncode == 17
    restarted = Ledger(path)
    assert restarted.rows()[0]["state"] == "possibly_sent"
    for key in ("crashed", "new-key"):
        with pytest.raises(BudgetStop):
            send(restarted, key)
    assert len(restarted.rows()) == 1


def test_cross_worker_atomic_total_cap(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", total_cap=1)
    for run in ("r", "s"):
        ledger.register(run)

    def worker(run):
        try:
            send(ledger, run=run)
            return "sent"
        except BudgetStop:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, ("r", "s")))
    assert sorted(result) == ["blocked", "sent"]
    assert len(ledger.rows()) == 1


@pytest.mark.parametrize("case", ["scope", "stream", "output", "input", "unregistered", "endpoint"])
def test_pre_send_rejections_do_not_touch_inner_transport(tmp_path, case):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")
    value = payload()
    if case == "stream":
        value["stream"] = True
    if case == "output":
        value["max_completion_tokens"] = 1
    if case == "input":
        value["messages"] = [{"role": "user", "content": "x" * 131073}]
    calls = []

    def handler(request):
        calls.append(request)
        return mock_response(request)

    with (
        httpx.Client(transport=GuardTransport(ledger, httpx.MockTransport(handler))) as client,
        request_scope(
            "wrong" if case == "unregistered" else "r",
            "one",
            "invalid" if case == "scope" else "ordinary",
        ),
        pytest.raises(BudgetStop),
    ):
        client.post(
            "https://real.invalid"
            if case == "endpoint"
            else "https://provider.invalid/v1/chat/completions",
            json=value,
        )
    assert not calls and not ledger.rows()


def test_live_transport_impossible_and_contract_cannot_reset(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", total_cap=1)
    with pytest.raises(BudgetStop, match="live_transport_disabled"):
        GuardTransport(ledger, cast(Any, object()))
    with pytest.raises(BudgetStop, match="live_transport_disabled"):
        AsyncGuardTransport(ledger, cast(Any, object()))
    with pytest.raises(BudgetStop, match="frozen_budget_contract"):
        Ledger(tmp_path / "ledger.sqlite", total_cap=192)


async def test_maintenance_context_uses_same_production_model_transport(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")
    model, _, sync, asynchronous = build(ledger, mock_response)
    token = graph_review_scope.set("offline-review-window")
    try:
        with request_scope("r", "maintenance-graphreview", "maintenance"):
            await model.ainvoke([HumanMessage(content="offline maintenance")])
        assert ledger.rows()[0]["kind"] == "maintenance"
        # No outer scope means no bypass, even for a maintenance model.
        with pytest.raises((openai.APIError, BudgetStop)):
            await model.ainvoke([HumanMessage(content="unscoped")])
        assert len(ledger.rows()) == 1
    finally:
        graph_review_scope.reset(token)
        sync.close()
        await asynchronous.aclose()


def test_review_cannot_accept_model_generated_or_duplicate_reviewers():
    assert review_check({})["status"] == "BLOCKED"
    packet = json.loads((ROOT / "reviews/template.json").read_text(encoding="utf-8"))
    packet["status"] = "INDEPENDENT_REVIEW_ACCEPTED"
    for r in packet["reviews"]:
        r.update(reviewer_id="same-author", author_or_model_generated=True)
    assert review_check(packet)["status"] == "BLOCKED"


def test_frozen_24_runs_protocol_delivery_and_quality_separation():
    result = json.loads((ROOT / "results/rehearsal.json").read_text(encoding="utf-8"))
    assert len(result["rows"]) == 24 and result["provider_requests"] == 0
    for row in result["rows"]:
        assert row["model_requests"] == row["formal_executor_jobs"] + 1
        if row["arm"] != "C":
            assert not row["deliveries"]
    receipts = json.loads((ROOT / "results/request-ledger.json").read_text(encoding="utf-8"))[
        "rows"
    ]
    for path in (ROOT / "results/runs").glob("*/trace.json"):
        trace = json.loads(path.read_text(encoding="utf-8"))
        scores = summarize(trace, receipts)
        assert scores["false_intervention"] == "NOT_EVALUABLE"
        assert scores["currency"] == "NOT_AVAILABLE" and not scores["first_triggering_call_saved"]
        if trace["arm"] == "C" and trace["task"] == "H":
            assert scores["actual_input_positions"] and scores["has_post_notice_test"]


async def test_clone_tamper_rejected_and_other_run_cannot_read_artifact(tmp_path):
    a, b = make_isolated(tmp_path / "a", "A"), make_isolated(tmp_path / "b", "A")
    try:
        _, original = await seed(a, "a")
        _, clone = await seed(b, "b")
        assert equivalence(original, clone)["status"] == "PASS"
        clone["artifacts"][0]["raw_utf8"] += "tampered"
        with pytest.raises(AssertionError):
            equivalence(original, clone)
        from types import SimpleNamespace

        from agent_workbench.agent.context import RunContext

        result = await cast(Any, read_artifact).coroutine(
            runtime=SimpleNamespace(context=RunContext(toolbox=b, thread_id="b")),
            artifact_id=original["artifacts"][0]["record"]["id"],
        )
        assert json.loads(result)["status"] == "failed"
    finally:
        await close(a)
        await close(b)


async def test_hint_ignore_adjust_recovery_and_identity_isolation(tmp_path):
    source = json.loads((ROOT / "seeds/A/source.json").read_text(encoding="utf-8"))
    task = next(
        t
        for t in json.loads((ROOT / "frozen/tasks.json").read_text(encoding="utf-8"))
        if t["task_id"] == "A"
    )
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ignore = await run_one(tmp_path / "ignore", task, "C", source, ledger, recover=True)
    adjust = await run_one(tmp_path / "adjust", task, "C", source, ledger, react=True)
    assert len(ignore["deliveries"]) == len(adjust["deliveries"]) == 1
    assert len(ignore["tool_calls"]) > len(adjust["tool_calls"])
    assert ignore["identity"] != adjust["identity"]


async def test_notice_has_no_new_user_command_or_artifact_body():
    result = json.loads((ROOT / "results/rehearsal.json").read_text(encoding="utf-8"))
    d = next(r for r in result["rows"] if r["task"] == "A" and r["arm"] == "C")["deliveries"][0]
    text = json.dumps(d, ensure_ascii=False)
    assert d["role"] == "tool" and "不是用户的新指令" in text
    assert "可以继续原计划" in text
    assert "controlled response; status is not a vulnerability conclusion" not in text
    assert "verified_sources" in text


async def test_feedback_corruption_preserves_original_results(tmp_path):
    from types import SimpleNamespace

    from langchain_core.messages import ToolMessage

    ledger = FeedbackLedger(tmp_path, "run", "thread")
    ledger.enqueue({"material_key": "one"})
    next(ledger.root.glob("pending-*.json")).write_text("invalid json", encoding="utf-8")
    middleware = PassiveToolReference(SimpleNamespace(history_action_shadow=None), "C", ledger)
    request = SimpleNamespace(messages=[ToolMessage(content="{}", tool_call_id="unpaired")])
    result = object()

    async def handler(value):
        assert value is request
        return result

    assert await cast(Any, middleware).awrap_model_call(request, handler) is result
    assert await cast(Any, middleware).awrap_tool_call(request, handler) is result


def test_feedback_receipt_survives_fresh_process(tmp_path):
    ledger = FeedbackLedger(tmp_path, "r", "t")
    ledger.enqueue({"material_key": "test"})
    script = "from pathlib import Path; import sys; from experiments.history_action_reflection_preparation_v1.reflection import FeedbackLedger; assert FeedbackLedger(Path(sys.argv[1]),'r','t').claim_next() is not None"
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=20)
    assert FeedbackLedger(tmp_path, "r", "t").claim_next() is None


def test_run_identity_cap_cannot_reset_per_run_allowance(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    for i in range(24):
        ledger.register(str(i))
    with pytest.raises(BudgetStop, match="pilot_run_count"):
        ledger.register("new-run-bypass")
    ledger.register("0")


def test_native_overrun_is_recorded_and_stops_run(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.register("r")

    def handler(request):
        value = mock_response(request).json()
        value["usage"] = {"prompt_tokens": 262144, "completion_tokens": 1, "total_tokens": 262145}
        return httpx.Response(200, json=value)

    with pytest.raises(BudgetStop, match="native_token_exhausted"):
        send(ledger, handler=handler)
    assert json.loads(ledger.rows()[0]["usage"])["total_tokens"] == 262145
    with pytest.raises(BudgetStop, match="unresolved_or_failed"):
        send(ledger, "next")


async def test_provider_parser_error_does_not_cause_second_handler_invocation(tmp_path):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, ToolMessage

    class Request:
        def __init__(self):
            self.messages = [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "x", "args": {}, "id": "x", "type": "tool_call"}],
                ),
                ToolMessage(content="{}", tool_call_id="x"),
            ]

        def override(self, **kwargs):
            return SimpleNamespace(**kwargs)

    ledger = FeedbackLedger(tmp_path, "r", "t")
    ledger.enqueue({"material_key": "one", "instruction": "reference"})
    middleware = PassiveToolReference(SimpleNamespace(history_action_shadow=None), "C", ledger)
    calls = []

    async def handler(request):
        calls.append(request)
        raise ValueError("injected parser fault")

    with pytest.raises(ValueError, match="injected parser fault"):
        await cast(Any, middleware).awrap_model_call(Request(), handler)
    assert len(calls) == 1
    assert ledger.claim_next() is None


def test_atomic_shared_budget_across_fresh_python_processes(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path, total_cap=1)
    ledger.register("r")
    ledger.register("s")
    code = """
import sys,httpx
from pathlib import Path
from experiments.history_action_reflection_pilot_readiness_v1.budget import Ledger,BudgetStop,request_scope
ledger=Ledger(Path(sys.argv[1]),total_cap=1)
try:
 with request_scope(sys.argv[2],'same-decision'):
  ledger.reserve(httpx.Request('POST','https://provider.invalid/v1/chat/completions',json={'stream':False,'max_completion_tokens':16384}))
 print('reserved')
except BudgetStop:
 print('blocked')
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path), run],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for run in ("r", "s")
    ]
    outputs = []
    for process in processes:
        out, err = process.communicate(timeout=20)
        assert process.returncode == 0, err
        outputs.append(out.decode("utf-8").strip())
    assert sorted(outputs) == ["blocked", "reserved"]
    assert len(ledger.rows()) == 1


def test_quality_rejects_current_trigger_as_future_savings(monkeypatch):
    from experiments.history_action_reflection_pilot_readiness_v1 import evaluate

    trace = json.loads((ROOT / "results/runs/A-1-C/trace.json").read_text(encoding="utf-8"))
    receipts = json.loads((ROOT / "results/request-ledger.json").read_text(encoding="utf-8"))[
        "rows"
    ]
    # Stub the independent review gate only to test the separate temporal scoring guard.
    monkeypatch.setattr(evaluate, "review_check", lambda _: {"status": "PASS"})
    annotation = evaluate.annotation_template("test-only-hash")
    trigger = trace["deliveries"][0]["tool_call_id"]
    annotation.update(
        adjudicated=True,
        analysis_boundary_tool_call_id=trigger,
        worthless_subsequent_repeat_ids=[trigger],
    )
    with pytest.raises(ValueError, match="strictly_after"):
        evaluate.summarize(trace, receipts, annotation, verified_trace_hash="test-only-hash")


def test_quality_cannot_be_published_without_verified_trace_hash(monkeypatch):
    from experiments.history_action_reflection_pilot_readiness_v1 import evaluate

    trace = json.loads((ROOT / "results/runs/A-1-C/trace.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(evaluate, "review_check", lambda _: {"status": "PASS"})
    annotation = evaluate.annotation_template("unverified-hash")
    annotation.update(adjudicated=True, task_quality="claimed-success")
    result = evaluate.summarize(trace, [], annotation)
    assert result["task_quality"] == "NOT_EVALUABLE"
