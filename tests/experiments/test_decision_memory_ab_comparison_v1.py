"""A/B 对照离线验证；只有显式授权的单组入口允许真实 Provider。"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid5

import httpx
import pytest
from experiments.decision_memory_ab_comparison_v1 import harness as ab
from experiments.decision_memory_ab_comparison_v1 import metrics, prepare
from experiments.decision_memory_live_v1 import harness as old
from tests.experiments.test_decision_memory_live_preparation import MockPolicy
from tests.experiments.test_decision_memory_s14c_r3 import packed


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch, request):
    authorized = request.node.name == "test_authorized_baseline" and os.environ.get(
        "REXAK_MEMORY_AB_APPROVED_FREEZE"
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("live_network_forbidden_in_offline_checks")

    def no_secrets(*_args, **_kwargs):
        raise AssertionError("production_secrets_forbidden")

    if not authorized:
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_secrets)
    monkeypatch.setattr("agent_workbench.config.secrets.build_secret_store", no_secrets)


def wire(arm="A"):
    return json.loads((ab.ROOT / f"bundle/initial/{arm}.json").read_text(encoding="utf-8"))


def url():
    return old.load("config.json")["endpoint"] + "/chat/completions"


@pytest.mark.integration
async def test_both_full_loops_contexts_graphreview_and_fair_initial_requests(database, tmp_path):
    saved = {}
    for arm in ab.ARMS:
        policy = MockPolicy(f"S14-{arm}")
        gate = ab.WireGate(httpx.MockTransport(lambda request, p=policy: packed(p(request))), arm)
        result = await ab.run_case(database, gate, tmp_path / f"{arm}.json")
        assert result["status"] == "completed" and result["pending_nodes"] == []
        assert result["real_provider_requests"] == 0 and result["transport_mode"] == "mock"
        assert len(result["calls"]) == 4
        assert sum(c["phase"] == "agent" for c in result["calls"]) == 3
        assert sum(c["phase"] == "graph_review" for c in result["calls"]) == 1
        assert result["tool_loop_exercised"] and result["artifact_body_cycle_exercised"]
        assert result["database_name"].startswith("workbench_test_")
        assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(result)
        assert len(result["manifests"]) == len(result["calls"])
        for call, manifest in zip(result["calls"], result["manifests"], strict=True):
            system = call["request"]["messages"][0]["content"]
            assert "decision_blackboard/1" not in system and "decision_memory" not in manifest
            assert {s["function"]["name"] for s in call["request"]["tools"]} == (
                {"get_graph", "update_graph"}
                if call["phase"] == "graph_review"
                else {t.name for t in old.TOOLS}
            )
            if arm == "B":
                recent = manifest["benchmark_recent"]["projection"]
                assert len(recent["operations"]) == 5
                assert "BENCHMARK_RECENT_DATA" in system and old.encoded(recent) in system
                assert recent["rule"] == "last_5_complete_operations"
            else:
                assert "BENCHMARK_RECENT_DATA" not in system and "benchmark_recent" not in manifest
        initial = result["calls"][0]["request"]
        sid = UUID(old.load("history.json")["session_id"])
        normalized = json.loads(
            json.dumps(initial).replace(result["run_id"], str(uuid5(sid, f"S14-{arm}")))
        )
        assert normalized == old.load(f"initial/S14-{arm}.json")
        path = ab.ROOT / f"bundle/initial/{arm}.json"
        if path.exists():
            assert wire(arm) == initial
        else:
            assert not (ab.ROOT / "freeze.json").exists()
            old.write_json(path, initial, exclusive=True)
        summary = metrics.summarize(result)
        assert summary["search_history_calls"] == 1 and summary["A_direct_read_calls"] == 1
        assert all(
            t["native_matches_parsed"] and t["arguments_valid"] and t["local_success"]
            for t in summary["tool_trace"]
        )
        if not (ab.ROOT / "freeze.json").exists():
            old.write_json(ab.ROOT / f"preflight/{arm}.json", result)
        saved[arm] = result
    assert saved["A"]["source_snapshot_sha256"] == saved["B"]["source_snapshot_sha256"]
    assert saved["A"]["run_id"] != saved["B"]["run_id"]
    assert saved["A"]["thread_id"] != saved["B"]["thread_id"]
    assert saved["A"]["run_id"] not in {
        "08a44deb-3351-5d58-aeb1-044c03d495e5",
        "14047e5c-73c7-502d-aa89-4174c27eb90c",
        "a96f5ba7-ab33-5fdc-9c8b-7cb3d2b40a4a",
    }
    left = json.loads(
        json.dumps(saved["A"]["calls"][0]["request"]).replace(
            saved["A"]["run_id"], "NORMALIZED_RUN"
        )
    )
    right = json.loads(
        json.dumps(saved["B"]["calls"][0]["request"]).replace(
            saved["B"]["run_id"], "NORMALIZED_RUN"
        )
    )
    right["messages"][0]["content"] = right["messages"][0]["content"].split(
        "\nBENCHMARK_RECENT_DATA", 1
    )[0]
    assert left == right


@pytest.mark.integration
async def test_graphreview_keeps_original_one_format_repair(database, tmp_path):
    policy = MockPolicy("S14-A")
    reviews = 0

    def reply(request):
        nonlocal reviews
        schema_names = {t["function"]["name"] for t in json.loads(request.content)["tools"]}
        if schema_names == {"get_graph", "update_graph"}:
            reviews += 1
            if reviews == 1:
                return policy.response(
                    {"role": "assistant", "content": "离线格式修正前的正文。"}, "stop"
                )
        return policy(request)

    result = await ab.run_case(
        database, ab.WireGate(httpx.MockTransport(reply)), tmp_path / "repair.json"
    )
    assert result["status"] == "completed"
    assert reviews == sum(c["phase"] == "graph_review" for c in result["calls"]) == 2
    assert sum(c["phase"] == "agent" for c in result["calls"]) == 3


@pytest.mark.parametrize(
    "failure",
    [
        "length",
        "timeout",
        "bad_json",
        "bad_gzip",
        "http_500",
        "unknown_tool",
        "invalid_args",
        "duplicate_id",
    ],
)
@pytest.mark.integration
async def test_failure_stops_without_retry(database, tmp_path, failure):
    def reply(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("offline", request=request)
        if failure == "bad_json":
            return httpx.Response(200, content=b"{broken")
        if failure == "bad_gzip":
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"invalid")
            )
        data = MockPolicy("S14-A", "length" if failure == "length" else None)(request).json()
        call = data["choices"][0]["message"]["tool_calls"][0]
        if failure == "unknown_tool":
            call["function"]["name"] = "forbidden_tool"
        if failure == "invalid_args":
            call["function"]["arguments"] = '{"kind":["operation"]}'
        if failure == "duplicate_id":
            data["choices"][0]["message"]["tool_calls"].append(call)
        return httpx.Response(500 if failure == "http_500" else 200, json=data)

    gate = ab.WireGate(httpx.MockTransport(reply))
    result = await ab.run_case(database, gate, tmp_path / f"{failure}.json")
    assert result["status"] == "stopped" and len(gate.rows) == 1 and gate.stopped
    assert not result["actions"] and not result["tool_loop_exercised"]


@pytest.mark.parametrize("estimate,allowed", [(76171, True), (131072, True), (131073, False)])
async def test_same_131072_envelope(monkeypatch, estimate, allowed):
    sample = SimpleNamespace(
        estimated_input_tokens=estimate, to_dict=lambda: {"estimated_input_tokens": estimate}
    )
    monkeypatch.setattr(old, "estimate_request", lambda *_args, **_kwargs: sample)
    box = SimpleNamespace(experiment_unsent_requests=[], context_manifest={})
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=SimpleNamespace(toolbox=box)),
        messages=[],
        system_message=None,
        tools=[],
        model=None,
        model_settings={},
        tool_choice=None,
    )
    sent = []

    async def handler(value):
        sent.append(value)
        return "allowed"

    middleware = cast(Any, ab.RequestEnvelope())
    if allowed:
        assert await middleware.awrap_model_call(request, handler) == "allowed" and sent
    else:
        with pytest.raises(old.ExperimentLimit, match="experimental_request_envelope"):
            await middleware.awrap_model_call(request, handler)
        assert not sent and box.experiment_unsent_requests[0]["provider_sent"] is False


@pytest.mark.parametrize("arm", ["A", "B"])
async def test_durable_phase_caps_total_cap_and_restart(tmp_path, monkeypatch, arm):
    expected = wire(arm)
    monkeypatch.setattr(ab, "ROOT", tmp_path)
    old.write_json(tmp_path / f"bundle/initial/{arm}.json", expected)
    ab.claim_attempt("mock", arm)
    gate = ab.WireGate(
        httpx.MockTransport(
            lambda _request: MockPolicy("S14-A").response(
                {"role": "assistant", "content": "offline"}, "stop"
            )
        ),
        arm,
        durable=True,
    )
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        for _ in range(6):
            await client.post(url(), json=expected)
        with pytest.raises(old.ExperimentLimit, match="physical_request_budget"):
            await client.post(url(), json=expected)
        review = {
            **expected,
            "tools": [
                t
                for t in expected["tools"]
                if t["function"]["name"] in {"get_graph", "update_graph"}
            ],
        }
        token = old.graph_review_scope.set("offline-review")
        try:
            for _ in range(2):
                await client.post(url(), json=review)
            with pytest.raises(old.ExperimentLimit, match="physical_request_budget"):
                await client.post(url(), json=review)
        finally:
            old.graph_review_scope.reset(token)
    rows = ab.read_ledger(ab.results_for(arm) / "ledger.jsonl")
    reserved = [r for r in rows if r["record"] == "reserved"]
    assert len(reserved) == 8 and sum(r["phase"] == "agent" for r in reserved) == 6
    assert sum(r["phase"] == "graph_review" for r in reserved) == 2
    with pytest.raises(RuntimeError, match="already_attempted"):
        ab.claim_attempt("mock", arm)
    newer = ab.WireGate(httpx.MockTransport(MockPolicy("S14-A")), arm, durable=True)
    async with httpx.AsyncClient(transport=newer, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit, match="durable_physical"):
            await client.post(url(), json=expected)
    assert not newer.rows


async def test_combined_sixteen_cap_and_unknown_response_reservation(tmp_path, monkeypatch):
    expected = wire()
    monkeypatch.setattr(ab, "ROOT", tmp_path)
    old.write_json(tmp_path / "bundle/initial/A.json", expected)
    ab.claim_attempt("mock", "A")

    def timeout(request):
        raise httpx.ReadTimeout("offline unknown", request=request)

    gate = ab.WireGate(httpx.MockTransport(timeout), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(url(), json=expected)
    rows = ab.read_ledger(ab.results_for("A") / "ledger.jsonl")
    assert sum(r["record"] == "reserved" for r in rows) == 1
    assert (ab.results_for("A") / "STOP.json").exists()
    before = {p.name: p.read_bytes() for p in ab.results_for("A").iterdir()}
    with pytest.raises(RuntimeError, match="already_attempted"):
        ab.claim_attempt("mock", "A")
    assert before == {p.name: p.read_bytes() for p in ab.results_for("A").iterdir()}
    # 另一个临时位置只验证合计上限，不能影响任何真实组目录。
    other = tmp_path / "combined"
    monkeypatch.setattr(ab, "ROOT", other)
    old.write_json(other / "bundle/initial/A.json", expected)
    ab.claim_attempt("mock", "A")
    ledger = ab.results_for("B") / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text((json.dumps({"record": "reserved"}) + "\n") * 16, encoding="utf-8")
    gate = ab.WireGate(httpx.MockTransport(MockPolicy("S14-A")), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit, match="durable_physical"):
            await client.post(url(), json=expected)
    assert not gate.rows and gate.ledger is not None and not gate.ledger.exists()


async def test_wire_drift_rejected_before_reservation(tmp_path, monkeypatch):
    expected = wire()
    monkeypatch.setattr(ab, "ROOT", tmp_path)
    old.write_json(tmp_path / "bundle/initial/A.json", expected)
    ab.claim_attempt("mock", "A")
    gate = ab.WireGate(httpx.MockTransport(MockPolicy("S14-A")), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        for bad in (
            {**expected, "temperature": 0},
            {**expected, "tools": []},
            {**expected, "messages": [{"role": "user", "content": "changed"}]},
        ):
            with pytest.raises(old.ExperimentLimit):
                await client.post(url(), json=bad)
    assert not gate.rows and gate.ledger is not None and not gate.ledger.exists()


@pytest.mark.integration
async def test_sixth_agent_request_can_be_final(database, tmp_path):
    count = 0

    def reply(request):
        nonlocal count
        data = json.loads(request.content)
        policy = MockPolicy("S14-A")
        if {t["function"]["name"] for t in data["tools"]} == {"get_graph", "update_graph"}:
            return policy(request)
        count += 1
        if count == 6:
            return policy.response(
                {"role": "assistant", "content": "第六次普通请求自然结束。"}, "stop"
            )
        return policy.response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"six-final-{count}",
                        "type": "function",
                        "function": {
                            "name": "read_artifact",
                            "arguments": json.dumps({"artifact_id": metrics.A_ARTIFACT}),
                        },
                    }
                ],
            },
            "tool_calls",
        )

    result = await ab.run_case(
        database, ab.WireGate(httpx.MockTransport(reply)), tmp_path / "six-final.json"
    )
    assert result["status"] == "completed" and not result["pending_nodes"]
    assert count == sum(c["phase"] == "agent" for c in result["calls"]) == 6
    assert sum(c["phase"] == "graph_review" for c in result["calls"]) <= 2


@pytest.mark.integration
async def test_executor_once_with_mock_and_b_predecessor_guard(database, tmp_path, monkeypatch):
    candidate = wire()
    monkeypatch.setattr(ab, "ROOT", tmp_path)
    old.write_json(tmp_path / "bundle/initial/A.json", candidate)
    monkeypatch.setattr(prepare, "verify", lambda: "mock-hash")
    monkeypatch.setenv("REXAK_MEMORY_LIVE_API_KEY", "synthetic-unused")
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(MockPolicy("S14-A"))
    )
    with pytest.raises(RuntimeError, match="A_must_finish"):
        ab.audit_a_before_b()
    result = await ab.execute_authorized(database, "A", "mock-hash", 8)
    assert result["status"] == "completed" and result["real_provider_requests"] == 0
    assert ab.audit_a_before_b()["A_requests"] == len(result["calls"])
    before = {p.name: p.read_bytes() for p in ab.results_for("A").iterdir()}
    with pytest.raises(RuntimeError, match="already_attempted"):
        await ab.execute_authorized(database, "A", "mock-hash", 8)
    assert before == {p.name: p.read_bytes() for p in ab.results_for("A").iterdir()}


@pytest.mark.parametrize(
    "reason,permitted",
    [
        ("experimental_request_envelope_exceeded_no_trimming", True),
        ("physical_request_budget_exhausted", True),
        ("AttributeError", False),
    ],
)
def test_normal_group_stops_allow_b_but_infrastructure_defects_do_not(
    tmp_path, monkeypatch, reason, permitted
):
    monkeypatch.setattr(ab, "ROOT", tmp_path)
    path = ab.results_for("A")
    old.write_json(
        path / "result.json",
        {
            "run_id": ab.identity("A")["run_id"],
            "calls": [{"index": 1}],
            "real_provider_requests": 1,
            "transport_mode": "live",
            "status": "stopped",
            "stop_reason": reason,
        },
    )
    rows = [
        {"record": "reserved", "index": 1, "wire_sha256": "x"},
        {"record": "result", "index": 1, "wire_sha256": "x"},
        {"record": "trajectory", "status": "stopped"},
    ]
    (path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8"
    )
    if permitted:
        assert ab.audit_a_before_b()["A_status"] == "stopped"
    else:
        with pytest.raises(RuntimeError, match="unclassified_failure"):
            ab.audit_a_before_b()


def test_frozen_source_drift_prevents_launch(tmp_path, monkeypatch):
    root = tmp_path / "experiments/decision_memory_ab_comparison_v1"
    monkeypatch.setattr(prepare, "ROOT", root)
    monkeypatch.setattr(prepare, "REPO", tmp_path)
    monkeypatch.setattr(prepare, "verify_original", lambda: "old")
    monkeypatch.setattr(prepare, "source_files", lambda: {"source.py": "changed"})
    old.write_json(
        root / "freeze.json",
        {
            "canonical_root": "experiments/decision_memory_ab_comparison_v1",
            "canonical_root_absolute": str(root.resolve()),
            "identities": {a: ab.identity(a) for a in ab.ARMS},
            "base_freeze_sha256": "old",
            "parent_C_R3_freeze_sha256": prepare.PARENT_HASH,
            "source_files": {"source.py": "original"},
        },
    )
    with pytest.raises(RuntimeError, match="source_inventory"):
        prepare.launch("A", "anything", 8)


def test_c_original_metrics_and_oracle_unchanged():
    x = json.loads(
        (ab.ROOT.parent / "decision_memory_s14c_r3/live-results/S14-C-R3.json").read_text(
            encoding="utf-8"
        )
    )
    summary = metrics.summarize(x)
    assert summary["ordinary_requests"] == 3 and summary["graph_review_requests"] == 0
    assert summary["usage"]["all"]["reported_sums"]["prompt_tokens"] == 87529
    assert summary["usage"]["all"]["reported_sums"]["completion_tokens"] == 3260
    assert summary["executed_tool_calls"] == 6 and summary["max_input_estimate"] == 33447
    assert summary["A_Q_v1_simulations"] == summary["A_Q_v2_simulations"] == 1
    assert (ab.ROOT / "bundle/oracle.json").read_bytes() == (
        old.BUNDLE / "oracle.json"
    ).read_bytes()
    assert prepare.verify_preservation()["status"] == "PRESERVED"


def test_missing_usage_is_explicitly_unavailable():
    value = metrics.usage([{"index": 1, "usage": None}])
    assert value["complete_totals"]["prompt_tokens"] is None
    assert value["missing_fields_at_requests"]["prompt_tokens"] == [1]
    assert metrics.usage([])["complete_totals"]["prompt_tokens"] == 0


@pytest.mark.skipif(
    not os.environ.get("REXAK_AB_OFFLINE_ARM"), reason="独立 PostgreSQL 子进程接缝另行运行"
)
@pytest.mark.integration
async def test_offline_process_fixture(database):
    arm = os.environ["REXAK_AB_OFFLINE_ARM"]
    result = await ab.run_case(
        database,
        ab.WireGate(httpx.MockTransport(MockPolicy(f"S14-{arm}")), arm),
        ab.ROOT / f"preflight/process-{arm}.json",
    )
    assert result["status"] == "completed" and result["real_provider_requests"] == 0


@pytest.mark.skipif(
    not os.environ.get("REXAK_MEMORY_AB_APPROVED_FREEZE"), reason="离线阶段不发送真实 Provider 请求"
)
@pytest.mark.integration
async def test_authorized_baseline(database):
    result = await ab.execute_authorized(
        database,
        os.environ["REXAK_MEMORY_AB_ARM"],
        os.environ["REXAK_MEMORY_AB_APPROVED_FREEZE"],
        8,
    )
    # 单组正常停止同样保存为有效实验结果，不为取得 completed 重试。
    assert result["status"] in {"completed", "stopped"}
