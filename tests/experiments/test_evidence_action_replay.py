"""已保存真实请求的纯离线回放；不运行任何原实验入口。"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import socket
from pathlib import Path

import pytest
from experiments.evidence_action_control_v1.replay import (
    HISTORY_PATH,
    RESULT_PATHS,
    _hash,
    build_real_snapshots,
    build_recorded_offline_snapshots,
    build_snapshots,
)

A_ARTIFACT = "artifact:e00066fc-37ab-47bd-b7fc-69fa90216f66"
A_OPERATION = "operation:ab5afd5c-2030-48cf-b750-4bfd9bb71547"
U_OPERATION = "operation:3f3b012b-f123-45ca-b4fe-a44dfcfc47c5"
U_JOB = "3eb1c268-726a-46ae-90bc-034907bb716c"
ARCHIVE_ID = "26502187-3969-4682-9f9a-c7df6315085e"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline_replay_must_not_connect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture(scope="module")
def snapshots():
    return build_real_snapshots()


def source(arm="C"):
    return (
        json.loads(HISTORY_PATH.read_text(encoding="utf-8")),
        json.loads(RESULT_PATHS[arm].read_text(encoding="utf-8")),
    )


def select(snapshots, arm, physical, name=None, configuration=None):
    return next(
        item for item in snapshots
        if item.boundary["arm"] == arm and item.boundary["physical_request_index"] == physical
        and (name is None or item.candidate.name == name)
        and (configuration is None or item.candidate.arguments.get("configuration") == configuration)
    )


def evidence(snapshot, ref=A_ARTIFACT):
    return next(item for item in snapshot.evidence if item.record_ref == ref)


def action(snapshot, ref=A_OPERATION):
    return next(item for item in snapshot.historical_actions if item.record_ref == ref)


def rehash(result, index):
    result["calls"][index]["visible_request_sha256"] = _hash(result["calls"][index]["request"])


def test_real_candidate_inventory_and_provenance(snapshots):
    assert len(snapshots) == 16
    assert {arm: sum(item.boundary["arm"] == arm for item in snapshots) for arm in "ABC"} == {
        "A": 3, "B": 7, "C": 6,
    }
    assert len({item.snapshot_id for item in snapshots}) == 16
    for item in snapshots:
        assert item.snapshot_id.startswith(f"real-{item.boundary['arm']}-p")
        assert item.boundary["request_integrity_verified"] is True
        assert item.boundary["wire_sha256_recorded"]
        assert item.boundary["candidate_caller"] in item.candidate.source_refs
        assert item.boundary["future_results_used"] is False


def test_c_original_body_only_visible_after_first_request(snapshots):
    before = select(snapshots, "C", 1, "read_artifact")
    after = select(snapshots, "C", 2, "simulate_check", "v1")
    assert evidence(before).available is True
    assert evidence(before).visible is False
    assert evidence(before).content is None
    assert "configuration" not in action(before).conditions
    body = evidence(after)
    assert body.visible and body.complete and body.provenance_verified
    assert hashlib.sha256(body.content.encode("utf-8")).hexdigest() == body.sha256
    assert body.conditions == {
        "route": "A", "configuration": "v1", "entry": "/A", "environment": "Q", "method": "GET",
    }
    assert action(after).conditions["configuration"] == "v1"
    assert body.visibility_refs and "/calls/1/request/messages/6" in body.visibility_refs[0]


def test_c_same_batch_does_not_share_candidate_results(snapshots):
    first = select(snapshots, "C", 2, "simulate_check", "v1")
    second = select(snapshots, "C", 2, "simulate_check", "v2")
    assert first.historical_actions == second.historical_actions
    assert first.evidence == second.evidence
    batch = set(first.boundary["same_batch_tool_call_ids_excluded"])
    assert batch.isdisjoint(first.boundary["completed_tool_call_ids"])
    assert len(first.historical_actions) == 17
    assert all(item.tool_name != "simulate_check" for item in first.historical_actions)


def test_a_archive_pair_has_exact_original_hash(snapshots):
    before = select(snapshots, "A", 1, "read_artifact")
    after = select(snapshots, "A", 2, "update_graph")
    assert evidence(before).content is None
    body = evidence(after)
    assert body.provenance_verified and body.complete and body.visible
    assert body.operation_ref == A_OPERATION
    assert body.sha256 == "6d0caf161146bda062cc4189f5310ada9742d901850582989ce6f10f78d70854"
    assert "archive_tool_call:source-call-A" in body.visibility_refs
    assert any(ARCHIVE_ID in ref for ref in body.visibility_refs)
    assert before.candidate.estimated_cost["source_bytes"] == 125355
    assert "configuration" not in action(before).conditions


def test_b_never_receives_original_a_body(snapshots):
    for item in snapshots:
        if item.boundary["arm"] == "B":
            assert evidence(item).available
            assert evidence(item).visible is False
            assert evidence(item).content is None
            assert "configuration" not in action(item).conditions
    simulated = select(snapshots, "B", 3, "simulate_check", "v1")
    assert any(item.kind == "model_claim" and item.visible for item in simulated.evidence)
    assert all(not item.provenance_verified for item in simulated.evidence if item.kind == "model_claim")


def test_job_id_is_not_silently_converted_to_operation(snapshots):
    item = select(snapshots, "B", 1, "read_local_execution")
    assert item.candidate.arguments["operation_id"] == U_JOB
    assert item.candidate.operation_ref == f"operation:{U_JOB}"
    assert item.boundary["identity_namespace_mismatch"] == {
        "supplied_kind": "job", "supplied_id": U_JOB, "expected_kind": "operation",
        "related_operation_ref": U_OPERATION, "source_ref": f"job:{U_JOB}",
    }
    assert action(item, U_OPERATION).status == "unknown"
    assert action(item, U_OPERATION).job_states == {f"job:{U_JOB}": "unknown"}


def test_local_simulation_location_is_explicit(snapshots):
    item = select(snapshots, "C", 2, "simulate_check", "v1")
    assert item.candidate.conditions["executor_id"] == "control_local"
    assert item.candidate.conditions["execution_semantics"] == "local_simulator"
    assert action(item).conditions["executor_id"] == "island-Q"
    assert action(item).conditions["execution_semantics"] == "executor_http"
    assert "identity_id" not in action(item).conditions
    assert item.task.goal == "interpret_history"
    assert item.task.focus_conditions == {"route": "A", "environment": "Q"}


def test_future_final_messages_actions_and_response_do_not_change_earlier_snapshots():
    history, result = source()
    baseline = build_snapshots(history, result, "C", "recorded.json")
    result["messages"] = [{"content": "future asserted answer"}]
    result["actions"] = [{"operation_id": "future", "result": {"effect_observed": True}}]
    result["semantic_review"] = {"answer": "untrusted future oracle"}
    result["calls"][2]["request"]["messages"].append({"role": "tool", "content": "future"})
    result["calls"][1]["visible_response_message"]["content"] = "future speculation"
    polluted = build_snapshots(history, result, "C", "recorded.json")
    assert baseline == polluted


def test_candidate_id_results_in_request_are_excluded_even_if_polluted():
    history, result = source()
    target = result["calls"][1]
    tool = copy.deepcopy(result["calls"][2]["request"]["messages"][-2])
    assert tool["role"] == "tool"
    candidate_ai = copy.deepcopy(target["visible_response_message"])
    target["request"]["messages"].extend([candidate_ai, tool])
    rehash(result, 1)
    items = build_snapshots(history, result, "C", "polluted.json")
    candidate = select(items, "C", 2, "simulate_check", "v1")
    assert len(candidate.historical_actions) == 17
    assert set(candidate.boundary["same_batch_tool_call_ids_excluded"]).isdisjoint(
        candidate.boundary["completed_tool_call_ids"]
    )


@pytest.mark.parametrize("damage", ["unpaired", "body", "duplicate"])
def test_archive_tampering_cannot_promote_original_evidence(damage):
    history, result = source("A")
    target = result["calls"][1]
    message = next(item for item in target["request"]["messages"] if item.get("tool_call_id") == "call_37a1295786654c468aeed4a9")
    payload = json.loads(message["content"])
    archive = json.loads(payload["data"]["text"])
    if damage == "unpaired":
        next(item for item in archive["messages"] if item.get("id") == "source-ai-A")["tool_calls"] = []
    elif damage == "body":
        next(item for item in archive["messages"] if item.get("id") == "source-tool-A")["content"] = '{"effect_observed":true}'
    else:
        archive["messages"].append(copy.deepcopy(next(item for item in archive["messages"] if item.get("id") == "source-tool-A")))
    payload["data"]["text"] = json.dumps(archive, ensure_ascii=False)
    message["content"] = json.dumps(payload, ensure_ascii=False)
    rehash(result, 1)
    candidate = select(build_snapshots(history, result, "A", "damaged.json"), "A", 2)
    assert evidence(candidate).visible is False
    assert evidence(candidate).content is None


def test_unverified_request_degrades_trust():
    history, result = source()
    result["calls"][1]["visible_request_sha256"] = "0" * 64
    candidate = select(build_snapshots(history, result, "C", "damaged.json"), "C", 2)
    assert candidate.boundary["request_integrity_verified"] is False
    assert not evidence(candidate).provenance_verified
    assert "configuration" not in action(candidate).conditions


def test_original_bytes_and_attribution_required():
    history, result = source()
    artifact_id = A_ARTIFACT.split(":", 1)[1]
    history["artifact_bytes_base64"][artifact_id] = base64.b64encode(b"wrong").decode()
    candidate = select(build_snapshots(history, result, "C", "damaged.json"), "C", 2)
    assert not evidence(candidate).provenance_verified
    assert "configuration" not in action(candidate).conditions


def test_replay_is_deterministic_and_preserves_all_original_files():
    paths = [HISTORY_PATH, *RESULT_PATHS.values()]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    history, result = source()
    before_inputs = copy.deepcopy((history, result))
    first = build_snapshots(history, result, "C", "source.json")
    second = build_snapshots(history, result, "C", "source.json")
    assert first == second
    assert (history, result) == before_inputs
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_incomplete_history_degrades_without_invented_facts():
    _, result = source()
    items = build_snapshots({}, result, "C", "incomplete.json")
    assert len(items) == 6
    assert not select(items, "C", 2).historical_actions
    assert not any(item.provenance_verified for item in select(items, "C", 2).evidence)


def test_no_oracle_read_is_required(monkeypatch):
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert "oracle" not in str(path).lower()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert len(build_real_snapshots()) == 16


@pytest.mark.parametrize("malformed", [
    {"id": "bad", "function": []},
    {"id": "bad", "function": {"name": "simulate_check", "arguments": "[]"}},
    {"id": "bad", "function": {"name": "simulate_check", "arguments": "not json"}},
    {"id": "", "function": {"name": "get_graph", "arguments": "{}"}},
    7,
])
def test_malformed_candidate_is_diagnosed_and_never_upgraded(malformed):
    history, result = source()
    result["calls"][0]["visible_response_message"]["tool_calls"] = [malformed]
    item = build_snapshots(history, result, "C", "malformed.json")[0]
    assert item.boundary["candidate_input_valid"] is False
    assert item.boundary["candidate_parse_diagnostics"]
    assert item.candidate.kind == "other"
    assert item.candidate.conditions == {}


def test_duplicate_candidate_ids_are_diagnosed_without_identity_collision():
    history, result = source()
    call = result["calls"][0]["visible_response_message"]["tool_calls"][0]
    result["calls"][0]["visible_response_message"]["tool_calls"] = [call, copy.deepcopy(call)]
    items = build_snapshots(history, result, "C", "duplicate.json")[:2]
    assert len({item.snapshot_id for item in items}) == 2
    assert all(not item.boundary["candidate_input_valid"] for item in items)


def test_uuid_occurrence_does_not_imply_execution_state_visible(snapshots):
    item = select(snapshots, "B", 3)
    assert A_OPERATION in item.boundary["identifier_navigation_refs"]
    metadata = evidence(item, f"metadata:{A_OPERATION}")
    assert metadata.available and not metadata.visible and metadata.content is None


def test_structured_c_board_metadata_visible_without_original_body(snapshots):
    item = select(snapshots, "C", 1)
    metadata = evidence(item, f"metadata:{A_OPERATION}")
    assert metadata.visible and metadata.provenance_verified and not metadata.complete
    assert metadata.content is not None
    payload = json.loads(metadata.content)
    assert payload["execution"]["operation_status"] == "succeeded"
    assert payload["statement"]["result_fields"] == {"status_code": 200}
    assert "control_ok" not in metadata.content
    assert "/messages/0/board/relevant_evidence/" in metadata.visibility_refs[0]
    assert evidence(item).content is None and not evidence(item).visible


def test_tampered_request_c_board_cannot_establish_verified_metadata():
    history, result = source()
    result["calls"][0]["visible_request_sha256"] = "0" * 64
    item = select(build_snapshots(history, result, "C", "bad-board.json"), "C", 1)
    metadata = evidence(item, f"metadata:{A_OPERATION}")
    assert metadata.visible and not metadata.provenance_verified
    assert evidence(item).content is None


@pytest.mark.parametrize("malformed_data", [[], "not an object", None])
def test_malformed_tool_result_degrades_safely(malformed_data):
    history, result = source()
    message = result["calls"][1]["request"]["messages"][6]
    payload = json.loads(message["content"])
    payload["data"] = malformed_data
    message["content"] = json.dumps(payload)
    rehash(result, 1)
    item = select(build_snapshots(history, result, "C", "malformed.json"), "C", 2)
    assert evidence(item).content is None
    assert not evidence(item).provenance_verified


def test_recorded_s15_s17_scripted_boundaries_are_distinct_from_real_samples():
    items = build_recorded_offline_snapshots()
    assert len(items) == 8
    assert {item.boundary["arm"] for item in items} == {"S15-C", "S17-C"}
    assert all(item.snapshot_id.startswith("offline-") for item in items)
    assert all(item.boundary["sample_origin"] == "recorded_offline_script" for item in items)
    assert all(item.boundary["candidate_reconstructed_from_recorded_ai"] for item in items)
    assert all(not item.boundary["request_integrity_verified"] for item in items)
    assert all("/request/messages/" in item.boundary["candidate_caller"] for item in items)


def test_s15_body_and_k_observation_change_visibility_without_confirming_b():
    items = build_recorded_offline_snapshots()
    before = select(items, "S15-C", 2, "read_artifact")
    simulate = select(items, "S15-C", 5, "simulate_check")
    b_ref = "artifact:296336ec-73da-45a4-96bd-5ebfcc01dc32"
    k_ref = "artifact:84a1edac-4de5-4f27-9732-5dd5fbced02d"
    b_operation = "operation:9eca06c7-892a-4e73-aa2e-5b6b9a7a1eb2"
    assert before.task.goal == simulate.task.goal == "progress_blocked"
    assert not evidence(before, b_ref).visible
    b_body, k_body = evidence(simulate, b_ref), evidence(simulate, k_ref)
    assert b_body.visible and b_body.complete and b_body.provenance_verified
    assert isinstance(b_body.content, str) and isinstance(k_body.content, str)
    assert b_body.conditions["prerequisites"] == {"K": False}
    assert json.loads(b_body.content)["effect_checked"] is False
    assert k_body.visible and k_body.provenance_verified
    assert k_body.conditions["prerequisites"] == {"K": True}
    assert action(simulate, b_operation).conditions["prerequisites"] == {"K": False}
    assert "effect_observed" not in json.loads(k_body.content)
    assert not any(item.tool_name == "simulate_check" for item in simulate.historical_actions)


def test_s17_independent_success_does_not_rewrite_original_unknown():
    items = build_recorded_offline_snapshots()
    relevant = [item for item in items if item.boundary["arm"] == "S17-C"]
    assert len(relevant) == 3
    for item in relevant:
        assert item.task.goal == "investigate_unknown"
        assert action(item, U_OPERATION).status == "unknown"
        assert action(item, U_OPERATION).job_states[f"job:{U_JOB}"] == "unknown"
        assert action(item, "operation:559de09b-cb40-4931-ab27-b62f3e3d8220").status == "succeeded"
    before = select(items, "S17-C", 2, "read_local_execution")
    after = select(items, "S17-C", 3, "search_history")
    assert not any(item.kind == "execution_state" and item.visible for item in before.evidence)
    state = evidence(after, f"state:{U_OPERATION}")
    assert state.visible and state.provenance_verified
    assert isinstance(state.content, str)
    assert json.loads(state.content)["operation_status"] == "unknown"
