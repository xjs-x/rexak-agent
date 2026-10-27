"""R2 独立 Mock 验收；原 S14-C 文件永远只读。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

import httpx
import pytest
from experiments.decision_memory_live_v1 import harness as old
from experiments.decision_memory_s14c_r2 import harness as r2
from experiments.decision_memory_s14c_r2 import prepare
from langchain_core.messages import HumanMessage
from tests.experiments.test_decision_memory_live_preparation import MockPolicy


@pytest.fixture(autouse=True)
def no_network_or_secrets(monkeypatch, request):
    authorized = request.node.name == "test_authorized_r2" and os.environ.get(
        "REXAK_MEMORY_R2_APPROVED_FREEZE"
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("real_network_forbidden_in_offline_preflight")

    def secret(*args, **kwargs):
        raise AssertionError("production_secrets_forbidden")

    if not authorized:
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", secret)
    monkeypatch.setattr("agent_workbench.config.secrets.build_secret_store", secret)


def packed(response, encoding="gzip"):
    body = response.content
    compressed = gzip.compress(body, mtime=0) if encoding == "gzip" else zlib.compress(body)
    return httpx.Response(
        200,
        headers={
            "content-encoding": encoding,
            "content-type": "application/json",
            "content-length": str(len(compressed)),
        },
        content=compressed,
    )


def wire():
    path = r2.ROOT / "bundle/initial.json"
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else old.load("initial/S14-C.json")
    )


def url():
    return old.load("config.json")["endpoint"] + "/chat/completions"


class MultiPolicy(MockPolicy):
    """合成工具批次，固定历史仅供协议测试，不充当真实模型结果。"""

    def __call__(self, request):
        request_wire = json.loads(request.content)
        if not hasattr(self, "wires"):
            self.wires = []
        self.wires.append(request_wire)
        self.index += 1
        ids = old.load("history.json")["fixture_ids"]
        if self.index == 1:
            names_args = [
                ("read_artifact", {"artifact_id": ids["A_artifact"]}),
                ("read_local_execution", {"operation_id": ids["A"]}),
                ("read_local_execution", {"operation_id": ids["U"]}),
                ("read_artifact", {"artifact_id": ids["B_artifact"]}),
            ]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"r2-multi-{i}",
                        "type": "function",
                        "function": {"name": n, "arguments": json.dumps(a)},
                    }
                    for i, (n, a) in enumerate(names_args)
                ],
            }
            return packed(self.response(message, "tool_calls"))
        latest = request_wire["messages"][-4:]
        assert {m["tool_call_id"] for m in latest} == {f"r2-multi-{i}" for i in range(4)}
        assert all(m["role"] == "tool" for m in latest)
        return packed(
            self.response({"role": "assistant", "content": "离线多工具回传后自然结束。"}, "stop")
        )


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_legacy_decoding_failure_and_direct_adapter_success(encoding):
    policy = MockPolicy("S14-C")

    def reply(request):
        return packed(policy(request), encoding)

    gate = old.WireGate(httpx.MockTransport(reply), "S14-C")
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        model = old.model_for(client).bind_tools(old.TOOLS)
        with pytest.raises(Exception) as raised:
            await model.ainvoke([HumanMessage(content="离线复现")])
    chain = r2.exception_chain(raised.value)
    types = [c["type"] for c in chain]
    assert types[:3] == ["OpenAIConnectionError", "APIConnectionError", "DecodingError"]
    assert gate.rows[0]["http_status"] == 200
    assert gate.rows[0]["finish_reason"] == "tool_calls"
    assert gate.rows[0]["error_category"] == "DecodingError"
    assert any(
        f["function"] == "handle_async_request" and f["file"].endswith("harness.py")
        for f in chain[2]["frames"]
    )
    if not (r2.ROOT / "freeze.json").exists():
        old.write_json(
            r2.ROOT / f"preflight/legacy-{encoding}.json",
            {"synthetic_only": True, "exception_chain": chain, "transport_row": gate.rows[0]},
        )
    # 相同压缩响应直接进入未修改的生产适配器：合法参数和 reasoning 回传均通过。
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply), trust_env=False) as client:
        message = (
            await old.model_for(client)
            .bind_tools(old.TOOLS)
            .ainvoke([HumanMessage(content="离线复现")])
        )
    assert message.tool_calls and not message.invalid_tool_calls
    assert message.additional_kwargs["reasoning_content"] == "PRIVATE_REASONING_SENTINEL"


@pytest.mark.integration
async def test_r2_full_multi_tool_loop_natural_end_manifest_and_identity(database, tmp_path):
    policy = MultiPolicy("S14-C")
    gate = r2.WireGate(httpx.MockTransport(policy))
    result = await r2.run_case(database, gate, tmp_path / "result.json")
    assert result["status"] == "completed" and not result["pending_nodes"]
    assert (
        len(result["calls"]) == 2
        and result["tool_loop_exercised"]
        and result["artifact_body_cycle_exercised"]
    )
    assert result["run_id"] != "08a44deb-3351-5d58-aeb1-044c03d495e5"
    assert result["thread_id"].startswith("memory-live-S14-C-R2-")
    assert result["real_provider_requests"] == 0
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(result)
    assert len(result["manifests"]) == len(result["calls"])
    for call, manifest in zip(result["calls"], result["manifests"], strict=True):
        assert (
            json.dumps(manifest["decision_memory"]["projection"], ensure_ascii=False)
            in call["request"]["messages"][0]["content"]
        )
        from agent_workbench.models.context_budget import serialized

        assert (
            hashlib.sha256(serialized(policy.wires[call["index"] - 1]).encode()).hexdigest()
            == call["wire_sha256"]
        )
        assert (
            hashlib.sha256(serialized(call["request"]).encode()).hexdigest()
            == call["visible_request_sha256"]
        )
        # 首请求无回传隐藏字段，可直接用已导出请求重算完整 wire 哈希。
        if call["index"] == 1:
            assert (
                hashlib.sha256(serialized(call["request"]).encode()).hexdigest()
                == call["wire_sha256"]
            )
    assert len(result["messages"][-6]["data"]["tool_calls"]) == 4
    assert all(c["status"] == "http_response_validated" for c in result["calls"])
    path = r2.ROOT / "bundle/initial.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == result["calls"][0]["request"]
    else:
        assert not (r2.ROOT / "freeze.json").exists()
        old.write_json(path, result["calls"][0]["request"], exclusive=True)
    legacy_request = old.load("initial/S14-C.json")
    normalized = json.loads(
        json.dumps(result["calls"][0]["request"]).replace(
            r2.identity()["run_id"], "08a44deb-3351-5d58-aeb1-044c03d495e5"
        )
    )
    assert normalized == legacy_request
    # 证据文件只在 seal 前创建；seal 后重跑测试不改冻结证据。
    if not (r2.ROOT / "freeze.json").exists():
        old.write_json(r2.ROOT / "preflight/multi-tool-natural-end.json", result)
    with pytest.raises(RuntimeError, match="result_exists"):
        await r2.run_case(database, gate, tmp_path / "result.json")


@pytest.mark.integration
async def test_natural_end_without_tools_is_preserved(database, tmp_path):
    def stop(request):
        return MockPolicy("S14-C").response(
            {"role": "assistant", "content": "离线自然结束。"}, "stop"
        )

    result = await r2.run_case(
        database, r2.WireGate(httpx.MockTransport(stop)), tmp_path / "no-tools.json"
    )
    assert result["status"] == "completed" and not result["pending_nodes"]
    assert len(result["calls"]) == 1 and not result["tool_loop_exercised"]


@pytest.mark.parametrize(
    "failure",
    [
        "length",
        "timeout",
        "invalid_json",
        "invalid_args",
        "unknown_tool",
        "duplicate_id",
        "bad_gzip",
        "http_500",
    ],
)
@pytest.mark.integration
async def test_stop_classification_no_retry_or_tool_execution(database, tmp_path, failure):
    def reply(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"{broken")
        if failure == "bad_gzip":
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"invalid gzip")
            )
        response = MockPolicy("S14-C", "length" if failure == "length" else None)(request)
        data = response.json()
        call = data["choices"][0]["message"]["tool_calls"][0]
        if failure == "invalid_args":
            call["function"]["arguments"] = '{"kind":["operation"],"query":"/A"}'
        if failure == "unknown_tool":
            call["function"]["name"] = "unknown_tool"
        if failure == "duplicate_id":
            data["choices"][0]["message"]["tool_calls"].append(call)
        return httpx.Response(500 if failure == "http_500" else 200, json=data)

    gate = r2.WireGate(httpx.MockTransport(reply))
    result = await r2.run_case(database, gate, tmp_path / f"{failure}.json")
    assert result["status"] == "stopped" and len(gate.rows) == 1 and gate.stopped
    assert not result["actions"] and not result["tool_loop_exercised"]
    assert gate.rows[0]["status"] == (
        "timeout"
        if failure == "timeout"
        else "http_error"
        if failure == "http_500"
        else "protocol_error"
    )
    assert result["exception_chain"]
    if failure == "length":
        assert gate.rows[0]["finish_reason"] == "length"
    if not (r2.ROOT / "freeze.json").exists():
        old.write_json(r2.ROOT / f"preflight/stop-{failure}.json", result)


@pytest.mark.parametrize(
    "arguments", ["{", "[]", "null", "42", {"kind": "operation"}, '{"limit":"not-an-integer"}']
)
async def test_illegal_arguments_fail_closed(arguments):
    def reply(request):
        data = MockPolicy("S14-C")(request).json()
        data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
        return httpx.Response(200, json=data)

    gate = r2.WireGate(httpx.MockTransport(reply))
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit, match="invalid_arguments"):
            await client.post(url(), json=wire())
    assert gate.stopped and len(gate.rows) == 1


async def test_durable_identity_reservation_cap_and_no_path_bypass(tmp_path, monkeypatch):
    monkeypatch.setattr(r2, "ROOT", tmp_path)
    old.write_json(tmp_path / "bundle/initial.json", old.load("initial/S14-C.json"))
    r2.claim_attempt("mock-only")
    with pytest.raises(RuntimeError, match="already_attempted"):
        r2.claim_attempt("mock-only")
    gate = r2.WireGate(httpx.MockTransport(MockPolicy("S14-C")), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        for _ in range(6):
            await client.post(url(), json=wire())
        with pytest.raises(old.ExperimentLimit, match="physical_request_budget"):
            await client.post(url(), json=wire())
    rows = r2.read_ledger(tmp_path / "live-results/ledger.jsonl")
    assert sum(r["record"] == "reserved" for r in rows) == 6
    assert sum(r["record"] == "result" for r in rows) == 6
    assert all(r["case"] == r2.CASE and r["run_id"] == r2.identity()["run_id"] for r in rows)
    assert rows[0]["wire_sha256"] == rows[1]["wire_sha256"]
    gate2 = r2.WireGate(httpx.MockTransport(MockPolicy("S14-C")), durable=True)
    async with httpx.AsyncClient(transport=gate2, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit, match="durable_physical"):
            await client.post(url(), json=wire())
    assert not gate2.rows


async def test_campaign_cap_counts_original_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(r2, "ROOT", tmp_path / "r2")
    r2.ROOT.mkdir()
    old.write_json(r2.ROOT / "bundle/initial.json", old.load("initial/S14-C.json"))
    legacy = tmp_path / "legacy"
    ledger = legacy / "live-results/ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text((json.dumps({"record": "reserved"}) + "\n") * 66, encoding="utf-8")
    monkeypatch.setattr(old, "ROOT", legacy)
    r2.claim_attempt("mock-only")
    gate = r2.WireGate(httpx.MockTransport(MockPolicy("S14-C")), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit, match="durable_physical"):
            await client.post(url(), json=wire())
    assert gate.ledger is not None
    assert not gate.rows and not gate.ledger.exists()


async def test_stop_and_ledger_immutable_after_protocol_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(r2, "ROOT", tmp_path)
    old.write_json(tmp_path / "bundle/initial.json", old.load("initial/S14-C.json"))
    r2.claim_attempt("mock-only")
    gate = r2.WireGate(httpx.MockTransport(MockPolicy("S14-C", "length")), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit):
            await client.post(url(), json=wire())
    before = {p.name: p.read_bytes() for p in (tmp_path / "live-results").iterdir()}
    with pytest.raises(RuntimeError, match="already_attempted"):
        r2.claim_attempt("mock-only")
    another = r2.WireGate(httpx.MockTransport(MockPolicy("S14-C")), durable=True)
    async with httpx.AsyncClient(transport=another, trust_env=False) as client:
        with pytest.raises(old.ExperimentLimit, match="claim_or_stop"):
            await client.post(url(), json=wire())
    assert before == {p.name: p.read_bytes() for p in (tmp_path / "live-results").iterdir()}


async def test_outbound_contract_and_first_request_checked_before_reservation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(r2, "ROOT", tmp_path)
    expected = old.load("initial/S14-C.json")
    old.write_json(tmp_path / "bundle/initial.json", expected)
    r2.claim_attempt("mock-only")
    gate = r2.WireGate(httpx.MockTransport(MockPolicy("S14-C")), durable=True)
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        for bad in (
            {**expected, "max_completion_tokens": 4096},
            {**expected, "tools": []},
            {**expected, "messages": [{"role": "user", "content": "changed"}]},
        ):
            with pytest.raises(old.ExperimentLimit):
                await client.post(url(), json=bad)
    assert gate.ledger is not None
    assert not gate.rows and not gate.ledger.exists()


def test_original_materials_preserved():
    assert prepare.verify_preservation()["status"] == "PRESERVED"


@pytest.mark.parametrize(
    "reasoning", [None, "synthetic reasoning", {"synthetic": "alternative field"}]
)
async def test_native_conversion_and_malformed_json_are_not_content_decoding_errors(reasoning):
    def reply(request):
        data = MockPolicy("S14-C")(request).json()
        message = data["choices"][0]["message"]
        message["reasoning_content"] = reasoning
        message["tool_calls"][0]["function"]["arguments"] = "{broken"
        return packed(httpx.Response(200, json=data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply), trust_env=False) as client:
        message = (
            await old.model_for(client)
            .bind_tools(old.TOOLS)
            .ainvoke([HumanMessage(content="合成解析负例")])
        )
    assert not message.tool_calls and len(message.invalid_tool_calls) == 1
    if isinstance(reasoning, str):
        assert message.additional_kwargs["reasoning_content"] == reasoning


@pytest.mark.integration
async def test_tool_batch_limit_blocks_before_any_execution(database, tmp_path):
    def reply(request):
        policy = MockPolicy("S14-C")
        data = policy(request).json()
        base = data["choices"][0]["message"]["tool_calls"][0]
        data["choices"][0]["message"]["tool_calls"] = [
            {**base, "id": f"over-budget-{i}"} for i in range(13)
        ]
        return httpx.Response(200, json=data)

    result = await r2.run_case(
        database, r2.WireGate(httpx.MockTransport(reply)), tmp_path / "batch-limit.json"
    )
    assert result["status"] == "stopped" and result["error_category"] == "ExperimentLimit"
    assert len(result["calls"]) == 1 and not result["actions"] and not result["tool_loop_exercised"]


@pytest.mark.integration
async def test_six_physical_requests_includes_final_reply_no_seventh_send(database, tmp_path):
    def reply(request):
        policy = MockPolicy("S14-C")
        data = policy(request).json()
        prior = sum(m["role"] == "tool" for m in json.loads(request.content)["messages"])
        data["choices"][0]["message"]["tool_calls"][0]["id"] = f"repeat-{prior}"
        return httpx.Response(200, json=data)

    gate = r2.WireGate(httpx.MockTransport(reply))
    result = await r2.run_case(database, gate, tmp_path / "six-limit.json")
    assert result["status"] == "stopped" and len(gate.rows) == 6
    assert not result["actions"]


@pytest.mark.integration
async def test_frozen_history_is_reloaded_without_previous_mock_state(database, tmp_path):
    def stop(request):
        return MockPolicy("S14-C").response({"role": "assistant", "content": "独立轨迹。"}, "stop")

    a = await r2.run_case(
        database, r2.WireGate(httpx.MockTransport(MultiPolicy("S14-C"))), tmp_path / "one.json"
    )
    b = await r2.run_case(database, r2.WireGate(httpx.MockTransport(stop)), tmp_path / "two.json")
    assert a["run_id"] == b["run_id"] == r2.identity()["run_id"]
    assert a["thread_id"] != b["thread_id"]
    assert a["source_snapshot_sha256"] == b["source_snapshot_sha256"]
    assert a["calls"][0]["request"] == b["calls"][0]["request"]
    assert not b["tool_loop_exercised"] and len(b["messages"]) == 5


def test_launch_requires_explicit_new_hash_cap_and_key(monkeypatch):
    monkeypatch.setattr(prepare, "verify", lambda: "mock-r2-hash")
    monkeypatch.delenv("REXAK_MEMORY_LIVE_API_KEY", raising=False)
    for approved, cap in (("old-hash", 6), ("mock-r2-hash", 7)):
        with pytest.raises(RuntimeError, match="explicit_r2_hash"):
            prepare.launch(approved, cap)
    with pytest.raises(RuntimeError, match="experiment_only_key"):
        prepare.launch("mock-r2-hash", 6)


@pytest.mark.integration
async def test_authorized_executor_entire_path_with_mock_only(database, tmp_path, monkeypatch):
    candidate = json.loads((r2.ROOT / "bundle/initial.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(r2, "ROOT", tmp_path)
    old.write_json(tmp_path / "bundle/initial.json", candidate)
    monkeypatch.setattr(prepare, "verify", lambda: "mock-r2-hash")
    monkeypatch.setenv("REXAK_MEMORY_LIVE_API_KEY", "synthetic-unused")
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(MultiPolicy("S14-C"))
    )
    result = await r2.execute_authorized(database, "mock-r2-hash", 6)
    assert result["status"] == "completed" and result["real_provider_requests"] == 0
    assert result["transport_mode"] == "mock" and result["thread_id"] == r2.identity()["thread_id"]
    rows = r2.read_ledger(tmp_path / "live-results/ledger.jsonl")
    assert [r["record"] for r in rows] == ["reserved", "result", "reserved", "result", "trajectory"]
    before = {p.name: p.read_bytes() for p in (tmp_path / "live-results").iterdir()}
    with pytest.raises(RuntimeError, match="already_attempted"):
        await r2.execute_authorized(database, "mock-r2-hash", 6)
    assert before == {p.name: p.read_bytes() for p in (tmp_path / "live-results").iterdir()}


def test_freeze_verifies_sources_dependency_bytes_and_versions(tmp_path, monkeypatch):
    root = tmp_path / "experiments/decision_memory_s14c_r2"
    monkeypatch.setattr(prepare, "ROOT", root)
    monkeypatch.setattr(prepare, "REPO", tmp_path)
    monkeypatch.setattr(prepare, "verify_original", lambda: "original-hash")
    monkeypatch.setattr(prepare, "verify_preservation", lambda: {})
    sources = {"synthetic.py": "source-hash"}
    monkeypatch.setattr(prepare, "source_files", lambda: dict(sources))
    module = Path(httpx.__file__).resolve()
    dependency = module.relative_to(Path(sys.prefix)).as_posix()
    dependencies = {
        "python": sys.version,
        "packages": {"httpx": "0.28.1"},
        "files": {dependency: prepare.sha(module)},
        "runtime_files": {},
    }
    old.write_json(root / "bundle/dependencies.json", dependencies)
    document = {
        "canonical_root": "experiments/decision_memory_s14c_r2",
        "canonical_root_absolute": str(root.resolve()),
        "identity": r2.identity(),
        "base_freeze_sha256": "original-hash",
        "source_files": dict(sources),
        "files": {
            "experiments/decision_memory_s14c_r2/bundle/dependencies.json": prepare.sha(
                root / "bundle/dependencies.json"
            )
        },
    }
    old.write_json(root / "freeze.json", document)
    assert prepare.verify() == prepare.sha(root / "freeze.json")
    sources["synthetic.py"] = "changed"
    with pytest.raises(RuntimeError, match="source_inventory"):
        prepare.verify()
    sources["synthetic.py"] = "source-hash"
    original_sha = prepare.sha
    monkeypatch.setattr(
        prepare, "sha", lambda path: "changed" if path.resolve() == module else original_sha(path)
    )
    with pytest.raises(RuntimeError, match="dependency_file"):
        prepare.verify()
    monkeypatch.setattr(prepare, "sha", original_sha)
    monkeypatch.setattr(prepare.importlib.metadata, "version", lambda _name: "wrong-version")
    with pytest.raises(RuntimeError, match="dependency_version"):
        prepare.verify()
    document["canonical_root_absolute"] = str(tmp_path / "another-copy")
    old.write_json(root / "freeze.json", document)
    with pytest.raises(RuntimeError, match="canonical_iteration_root"):
        prepare.verify()


@pytest.mark.skipif(
    not os.environ.get("REXAK_MEMORY_R2_APPROVED_FREEZE"), reason="R2 真实请求须下一轮明确批准"
)
@pytest.mark.integration
async def test_authorized_r2(database):
    result = await r2.execute_authorized(database, os.environ["REXAK_MEMORY_R2_APPROVED_FREEZE"], 6)
    assert result["status"] == "completed"
