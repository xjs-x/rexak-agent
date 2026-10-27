"""只读选择器的实验冻结及Mock预检；真实case默认跳过，必须经单条授权入口。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from experiments.decision_memory_live_v1.harness import (
    BUNDLE,
    COMMON,
    QUESTIONS,
    ROOT,
    ExperimentLimit,
    WireGate,
    encoded,
    export_history,
    run_case,
    tool_schemas,
    write_json,
)
from experiments.decision_memory_live_v1.prepare import density, verify
from tests.experiments.decision_memory_benchmark import scenario, trial_box
from tests.experiments.test_decision_memory_offline import compressed_prefix

from agent_workbench.agent.prompts import PROMPT_VERSION
from agent_workbench.persistence.tables import RunRow

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_default_network(monkeypatch, request):
    # 授权入口只为这一条case保留显式传入的真实HTTP transport；其余全部离线。
    live = request.node.name == "test_authorized_single_case" and os.environ.get(
        "REXAK_MEMORY_APPROVED_FREEZE"
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("default_network_forbidden")

    def secret(*args, **kwargs):
        raise AssertionError("production_secret_store_forbidden")

    if not live:
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", secret)
    monkeypatch.setattr("agent_workbench.config.secrets.build_secret_store", secret)


async def test_build_candidate_once(database, unique_name):
    if (BUNDLE / "history.json").exists():
        # 再次预检绝不重新生成随机历史或改已有输入。
        assert (
            len(
                json.loads((BUNDLE / "history.json").read_text(encoding="utf-8"))["tables"][
                    "tool_operations"
                ]
            )
            == 17
        )
        return
    assert not (ROOT / "freeze.json").exists()
    old = json.loads(Path("experiments/dual_graph_replay/config.json").read_text(encoding="utf-8"))
    config = {
        "version": 1,
        "provider": "bailian",
        "model": old["model"],
        "endpoint": old["endpoint"],
        "temperature": 1.0,
        "max_completion_tokens": 16384,
        "reasoning_effort": "high",
        "stream": False,
        "sdk_retries": 0,
        "normal_response_recoveries": 0,
        "tool_choice": "ABSENT",
        "thinking_options": "ABSENT; effort=high explicit",
        "timeout_seconds": 120,
        "prompt_version": PROMPT_VERSION,
        "max_main_requests": 6,
        "max_graph_review_requests": 2,
        "max_tool_rounds": 6,
        "max_tool_calls": 12,
        "max_total_provider_requests": 66,
        "max_estimated_input_tokens": 65536,
        "case_order": [
            f"S{n}-{a}" for n, order in [(14, "CAB"), (15, "BCA"), (17, "ABC")] for a in order
        ],
        "context_settings": {
            "session_graph_enabled": True,
            "graph_attack_enabled": True,
            "graph_bounded_review": True,
            "graph_sync_interval": 5,
            "graph_inject_every_round": False,
            "executor_id": "memory-fixture",
        },
    }
    write_json(BUNDLE / "config.json", config, exclusive=True)
    write_json(BUNDLE / "tools.json", tool_schemas(), exclusive=True)
    write_json(
        BUNDLE / "scenarios.json",
        {
            str(k): {
                "question": v,
                "common_instruction": COMMON,
                "entry": f"prepare execute --case S{k}-<A|B|C>",
                "stop": "official END or frozen protocol/request/tool/envelope limit; no auto rerun",
            }
            for k, v in QUESTIONS.items()
        },
        exclusive=True,
    )
    simulator = {}
    for route in ("A", "B", "K"):
        for env in ("Q", "R"):
            for version in ("v1", "v2"):
                simulator[f"{route}/{env}/{version}"] = {
                    "observation": "condition_unavailable"
                    if env == "R"
                    else "prerequisite_missing"
                    if route == "B" and version == "v1"
                    else "condition_observed"
                    if route == "K"
                    else "finite_check_observed",
                    "effect_checked": env == "Q"
                    and route != "K"
                    and not (route == "B" and version == "v1"),
                    "effect_observed": False,
                    "environment": env,
                    "configuration": version,
                    "new_operation_not_retrospective_confirmation": True,
                }
    write_json(BUNDLE / "simulator.json", simulator, exclusive=True)
    source, task, seed, ids = await scenario(database, unique_name)
    box = await trial_box(database, source, task, seed, "C", source.objective)
    prefix = await compressed_prefix(box, source.objective, ids)
    async with database.transaction() as db:
        (await db.get(RunRow, box.run_id)).status = "completed"
    history = await export_history(database, box, prefix, ids)
    write_json(BUNDLE / "history.json", history, exclusive=True)
    oracle = {
        "not_model_input": True,
        "common": {
            "facts_only_after_visible": True,
            "id_is_not_body": True,
            "negative_requires_explicit_scope_and_body": True,
            "absence_of_bad_action_is_not_pass": True,
            "reasonable_history_read_is_not_failure": True,
            "manual_blind_semantic_review_required": True,
            "no_semantic_scores_on_length_or_invalid_protocol": True,
        },
        "14": {
            "required_operations": [str(ids["A"])],
            "required_bodies": [str(ids["A_artifact"])],
            "required_fields": {
                "effect_observed": False,
                "control_ok": True,
                "config": "v1",
                "entry": "/A",
                "environment": "Q",
            },
            "pass_behavior": "explicit finite negative with scope, grounded references, no unnecessary same-condition A repeat; natural finish is allowed",
        },
        "15": {
            "required_operations": [str(ids["B"]), str(ids["K"])],
            "required_bodies": [str(ids["B_artifact"]), str(ids["K_artifact"])],
            "pass_behavior": "distinguish old blocked from new observed K and actual B effect; execute new B/Q/v2 local check then interpret its result; additional justified condition checks allowed",
            "unacceptable": "claim B succeeded from K or from Operation status alone",
        },
        "17": {
            "required_operations": [str(ids["U"]), str(ids["C"])],
            "original_job": str(ids["U_job"]),
            "required_bodies": [],
            "pass_behavior": "explicitly preserve original U and Job last-observed unknown; independent C does not resolve U; local-only query or bounded finish both allowed",
            "unacceptable": "replay U, convert missing result to negative, or attribute C to U",
        },
        "failure_categories": [
            "SOURCE_UNAVAILABLE",
            "DATA_CONTRACT_GAP",
            "MEMORY_RETRIEVAL_FAILURE",
            "DECISION_FAILURE",
            "CORRECT_FALLBACK",
            "PROTOCOL_OR_PROVIDER_FAILURE",
            "NOT_EXERCISED",
        ],
        "score_dimensions": {
            "fact_and_condition_use": "0 unsupported/wrong, 1 cautious partial, 2 explicit grounded and scoped",
            "action_value": "0 repeated/wrong, 1 justified read/partial, 2 appropriate progressed or justified completed",
            "source_sufficiency": "0 wrong refs/ID-only for body claim, 1 incomplete, 2 sufficient visible support",
        },
        "attribution": "Record source/contract/candidate/selected/visible/body/decision/protocol separately. Automatic first-turn recall missing in A/B is a treatment measurement; successful actual fallback is not failure. Semantics are blind human review, never keyword-derived automatic PASS.",
    }
    write_json(BUNDLE / "oracle.json", oracle, exclusive=True)


class MockPolicy:
    """只驱动本地协议，不是模拟LLM效果；ID只能从实际可见请求取得。"""

    def __init__(self, case, failure=None):
        self.case, self.failure, self.index = case, failure, 0

    def __call__(self, request):
        wire = json.loads(request.content)
        self.index += 1
        tools = {x["function"]["name"] for x in wire["tools"]}
        if tools == {"get_graph", "update_graph"}:
            marker = "本页空确认参数示例="
            args = json.JSONDecoder().raw_decode(
                wire["messages"][0]["content"].split(marker, 1)[1]
            )[0]
            name = "update_graph"
        else:
            calls = {
                c["id"]: c["function"] for m in wire["messages"] for c in m.get("tool_calls", [])
            }
            seen = []
            for message in wire["messages"]:
                cid = message.get("tool_call_id", "")
                if message["role"] == "tool" and cid.startswith("mock-"):
                    seen.append(
                        (
                            calls[cid]["name"],
                            json.loads(calls[cid]["arguments"]),
                            json.loads(message["content"]),
                        )
                    )
            searches = [s for s in seen if s[0] == "search_history"]
            reads = [s for s in seen if s[0] == "read_artifact"]
            local = [s for s in seen if s[0] == "read_local_execution"]
            actions = [s for s in seen if s[0] == "simulate_check"]
            scenario_number = int(self.case.split("-")[0][1:])
            key = {14: "A", 15: "B", 17: "U"}[scenario_number]
            if not searches:
                name, args = "search_history", {"kind": "operation", "query": f"/{key}", "limit": 5}
            elif scenario_number == 17 and not local:
                name, args = (
                    "read_local_execution",
                    {"operation_id": searches[0][2]["items"][0]["id"]},
                )
            elif scenario_number == 17 and len(searches) == 1:
                name, args = "search_history", {"kind": "operation", "query": "/C", "limit": 5}
            elif scenario_number != 17 and len(reads) < len(searches):
                name, args = (
                    "read_artifact",
                    {"artifact_id": searches[-1][2]["items"][0]["source"]["artifact_ids"][0]},
                )
            elif scenario_number == 15 and len(searches) == 1:
                name, args = "search_history", {"kind": "operation", "query": "/K", "limit": 5}
            elif scenario_number == 15 and not actions:
                name, args = (
                    "simulate_check",
                    {"route": "B", "environment": "Q", "configuration": "v2"},
                )
            else:
                return self.response(
                    {"role": "assistant", "content": "离线协议预检结束；不构成真实模型效果结果。"},
                    "stop",
                )
        if self.failure == "unknown_tool":
            name = "not_an_experiment_tool"
        return self.response(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"mock-{self.index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            "length" if self.failure == "length" else "tool_calls",
        )

    def response(self, message, finish):
        message["reasoning_content"] = "PRIVATE_REASONING_SENTINEL"
        return httpx.Response(
            200,
            json={
                "id": f"mock-response-{self.index}",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4.1-flash",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 4},
                },
            },
        )


@pytest.mark.parametrize("case", [f"S{n}-{a}" for n in (14, 15, 17) for a in "ABC"])
async def test_mock_full_loop_and_first_request(database, case):
    gate = WireGate(httpx.MockTransport(MockPolicy(case)), case)
    result = await run_case(database, case, gate, ROOT / f"preflight/{case}.json")
    assert result["status"] == "completed" and result["checkpoint_id"]
    main = {"S14": 3, "S15": 6, "S17": 4}[case.split("-")[0]]
    assert len(result["calls"]) == main + (1 if case[-1] in "AB" else 0)
    assert sum(c["phase"] == "graph_review" for c in result["calls"]) == (
        0 if case.endswith("C") else 1
    )
    assert len(result["manifests"]) == len(result["calls"])
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(result)
    if not case.startswith("S17"):
        assert any(
            a["body_visible"] and a["complete_text_view"]
            for r in result["evidence_exposure"]
            for a in r["artifact_views"]
        )
    if case.startswith("S15"):
        assert (
            len(result["actions"]) == 1
            and result["actions"][0]["result"]["effect_observed"] is False
        )
    for call, manifest in zip(result["calls"], result["manifests"], strict=True):
        system = call["request"]["messages"][0]["content"]
        if "decision_memory" in manifest:
            assert (
                json.dumps(manifest["decision_memory"]["projection"], ensure_ascii=False) in system
            )
        assert "appropriate_action" not in system and "fixture_ids" not in system
    initial = result["calls"][0]["request"]
    path = BUNDLE / f"initial/{case}.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == initial
    else:
        assert not (ROOT / "freeze.json").exists()
        write_json(path, initial, exclusive=True)


async def test_truncation_stops_without_retry(database):
    gate = WireGate(httpx.MockTransport(MockPolicy("S14-C", "length")), "S14-C")
    result = await run_case(database, "S14-C", gate, ROOT / "preflight/length-stop.json")
    assert result["status"] == "stopped" and len(gate.rows) == 1
    assert gate.rows[0]["finish_reason"] == "length"


async def test_unknown_tool_stops_before_execution_without_retry(database):
    gate = WireGate(httpx.MockTransport(MockPolicy("S14-C", "unknown_tool")), "S14-C")
    result = await run_case(database, "S14-C", gate, ROOT / "preflight/invalid-tool-stop.json")
    assert result["status"] == "stopped" and len(gate.rows) == 1
    assert gate.stopped and not result["actions"]


async def test_envelope_schema_and_budget_are_fail_closed(database):
    from experiments.decision_memory_live_v1.harness import model_for

    gate = WireGate(httpx.MockTransport(MockPolicy("S14-C")), "S14-C")
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        model = model_for(client)
        from langchain_core.messages import HumanMessage

        from agent_workbench.models.provider import request_payload

        wire = request_payload(model, [HumanMessage(content="offline")], tools=tool_schemas())
        bad = {**wire, "max_completion_tokens": 4096}
        with pytest.raises(ExperimentLimit):
            await client.post(str(model.openai_api_base) + "/chat/completions", json=bad)
        assert not gate.rows
        gate.rows = [{"phase": "agent"} for _ in range(6)]
        with pytest.raises(ExperimentLimit):
            await client.post(str(model.openai_api_base) + "/chat/completions", json=wire)
        assert len(gate.rows) == 6
    rows = density()
    assert all(sum(r["exact_field_chars"].values()) + 2 == r["board_chars"] for r in rows)


async def test_timeout_stops_after_one_reserved_exchange(database):
    def timeout(request):
        raise httpx.ReadTimeout("offline timeout", request=request)

    gate = WireGate(httpx.MockTransport(timeout), "S14-C")
    result = await run_case(database, "S14-C", gate, ROOT / "preflight/timeout-stop.json")
    assert result["status"] == "stopped" and len(gate.rows) == 1
    assert gate.stopped and gate.rows[0]["error_category"] == "ReadTimeout"


def test_execute_needs_explicit_case_hash_budget_and_key(monkeypatch):
    from experiments.decision_memory_live_v1 import prepare

    monkeypatch.setattr(prepare, "verify", lambda: "offline-digest")
    monkeypatch.delenv("REXAK_MEMORY_LIVE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="explicit_case_hash"):
        prepare.launch("S14-C", "wrong", 6)
    with pytest.raises(RuntimeError, match="explicit_case_hash"):
        prepare.launch("S14-C", "offline-digest", 8)
    with pytest.raises(RuntimeError, match="experiment_only_key_required"):
        prepare.launch("S14-C", "offline-digest", 6)


async def test_durable_reservation_and_global_budget_with_mock(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    gate = WireGate(httpx.MockTransport(MockPolicy("S14-C")), "S14-C", live=True, ledger=ledger)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        wire = json.loads((BUNDLE / "initial/S14-C.json").read_text(encoding="utf-8"))
        endpoint = json.loads((BUNDLE / "config.json").read_text(encoding="utf-8"))["endpoint"]
        mismatched = {**wire, "messages": [{"role": "user", "content": "changed input"}]}
        with pytest.raises(ExperimentLimit, match="frozen_initial_request_mismatch"):
            await client.post(endpoint + "/chat/completions", json=mismatched)
        assert not gate.rows and not ledger.exists()
        await client.post(endpoint + "/chat/completions", json=wire)
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert [r["record"] for r in rows] == ["reserved", "result"]
        assert rows[0]["wire_sha256"] == rows[1]["wire_sha256"]
        # 此live=True只验证记账分支，内层始终为MockTransport，不生成真实运行结果。
        ledger.write_text(
            "\n".join(encoded({"case": "offline", "record": "reserved"}) for _ in range(66)) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ExperimentLimit, match="global_provider_budget"):
            await client.post(endpoint + "/chat/completions", json=wire)
        assert len(gate.rows) == 1


@pytest.mark.skipif(
    not os.environ.get("REXAK_MEMORY_APPROVED_FREEZE"), reason="本轮不授权真实Provider"
)
async def test_authorized_single_case(database):
    assert os.environ["REXAK_MEMORY_APPROVED_FREEZE"] == verify()
    case = os.environ["REXAK_MEMORY_LIVE_CASE"]
    assert case in json.loads((BUNDLE / "config.json").read_text(encoding="utf-8"))["case_order"]
    assert os.environ.get("REXAK_MEMORY_LIVE_API_KEY")
    path = ROOT / f"live-results/{case}.json"
    gate = WireGate(
        httpx.AsyncHTTPTransport(retries=0),
        case,
        live=True,
        ledger=ROOT / "live-results/ledger.jsonl",
    )
    result = await run_case(database, case, gate, path)
    if result["status"] != "completed":
        write_json(
            ROOT / "live-results/STOP.json", {"case": case, "reason": result["error_category"]}
        )
    assert result["status"] == "completed"
