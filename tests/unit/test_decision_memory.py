"""只检验确定性合同，不把脚本输出当作模型规划能力。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_workbench.agent.decision_memory import focus_freshness
from agent_workbench.config.settings import Settings, settings_for_run
from agent_workbench.contracts.decision_memory import DecisionInput, MemorySnapshot
from agent_workbench.domain.decision_memory import compare_dimensions, select_memory


def operation(index, *, state="succeeded", step=None):
    key = f"operation:{uuid4()}"
    return {
        "record_ref": key,
        "run_id": f"old-run-{index}",
        "tool_call_id": f"c{index}",
        "tool_name": "http_request",
        "operation_status": state,
        "created_at": str(index),
        "access": "within_current_scope",
        "context": {"step_id": step},
        "dimensions": {"endpoint": f"http://fixture.invalid:80/path/{index}", "method": "GET"},
        "result": {"status_code": 200},
        "jobs": [],
        "resources": [],
    }


def request(**kw):
    return DecisionInput(session_id="s", run_id="r", control_version=1, state={}, **kw)


def test_old_structural_record_survives_recent_noise_and_claims_do_not_merge():
    old = operation(0, step="B")
    noise = [operation(i) for i in range(1, 31)]
    artifact = {
        "record_ref": f"artifact:{uuid4()}",
        "source_operation_ref": old["record_ref"],
        "availability": "available",
    }
    claims = tuple(
        {
            "record_ref": f"attempt:{uuid4()}@2",
            "origin_kind": "model_claim",
            "statement": {"action": "a_aux辅助目录读取", "outcome": outcome},
            "source_refs": [old["record_ref"]],
        }
        for outcome in ("negative", "blocked")
    )
    data = MemorySnapshot(
        boundary={"session_id": "s"},
        operations=(old, *noise),
        artifacts=(artifact,),
        claims=claims,
    )
    req = replace(
        request(focus_freshness="initial_input"),
        state={
            "current_focus": {"step_id": "B"},
            "plan": [{"step_id": "B", "status": "in_progress"}],
        },
    )
    board, trace = select_memory(req, data)
    assert len(board.relevant_evidence) == 1
    assert board.relevant_evidence[0].record_ref == old["record_ref"]
    assert artifact["record_ref"] in board.relevant_evidence[0].source_refs
    assert {x.statement["outcome"] for x in board.previous_attempts} == {"negative", "blocked"}
    assert all(x.support_status == "semantic_unverified" for x in board.previous_attempts)
    assert len([x for x in trace["candidates"] if not x["selected"]]) == 30
    assert board.retrieval_status["equivalence"] == "not_determined"


def test_unknown_not_resolved_by_independent_success_same_endpoint():
    old, new = operation(0, state="unknown"), operation(1)
    new["dimensions"] = dict(old["dimensions"])
    job = {
        "record_ref": f"job:{uuid4()}",
        "last_observed_state": "unknown",
        "sync_status": "uncertain",
        "executor_id": "island",
    }
    old["jobs"] = [job]
    board, _ = select_memory(
        request(explicit_refs=(old["record_ref"], new["record_ref"])),
        MemorySnapshot(boundary={"session_id": "s"}, operations=(old, new)),
    )
    assert len(board.relevant_evidence) == 2
    assert board.unresolved_results == [old["record_ref"]]
    assert (
        board.relevant_evidence[0].execution["jobs_last_observed"][0]["last_observed_state"]
        == "unknown"
    )
    assert not board.previous_attempts  # 不从执行状态自动制造Attempt。


def test_missing_keys_fallback_is_not_no_history_and_cross_session_rejected():
    data = MemorySnapshot(
        boundary={"session_id": "s"}, operations=tuple(operation(i) for i in range(8))
    )
    board, trace = select_memory(request(), data)
    assert board.retrieval_status["mode"] == "focus_ambiguous_navigation"
    assert len(board.relevant_evidence) == 5
    assert trace["candidates"][0]["association_status"] == "candidate_not_selected"
    with pytest.raises(ValueError, match="session_mismatch"):
        select_memory(request(), replace(data, boundary={"session_id": "another"}))


def test_condition_three_values_preserve_old_and_new_no_semantic_promotion():
    result = compare_dimensions(
        {"method": "GET", "auth_revision": 1, "environment": None},
        {"method": "GET", "auth_revision": 2, "environment": None},
        ["operation:old", "session:current"],
    )
    assert {x.field: x.status for x in result} == {
        "method": "comparable_same",
        "auth_revision": "changed",
        "environment": "insufficient",
    }
    assert result[0].source_refs == ["operation:old", "session:current"]


def test_focus_requires_explicit_successful_reexpression_not_echoed_receipt():
    focus = {"intent": "old"}
    state = {
        "current_focus": focus,
        "plan_revision": 3,
        "last_plan_change": {"run_id": "r", "source_tool_call_id": "p"},
    }
    messages = [
        HumanMessage(content="new", id="command-x"),
        AIMessage(
            content="", tool_calls=[{"id": "p", "name": "update_plan", "args": {"steps": []}}]
        ),
        ToolMessage(
            tool_call_id="p",
            content='{"ok":true,"plan_revision":3,"current_focus":{"intent":"old"}}',
        ),
    ]
    inputs = [{"record_ref": "command:x", "text": "new"}]
    assert focus_freshness(state, messages, inputs, 0, "r") == "unproven_after_user_input"
    messages[1].tool_calls[0]["args"]["focus"] = "old"
    assert focus_freshness(state, messages, inputs, 0, "r") == "reexpressed_after_user_input"
    messages[2].content = '{"ok":false}'
    assert focus_freshness(state, messages, inputs, 0, "r") == "unproven_after_user_input"


def test_run_mode_is_pinned_and_old_runs_ignore_environment_mode():
    settings = Settings(decision_memory_mode="blackboard_v1")
    assert settings_for_run(settings, {}).decision_memory_mode == "current"
    assert (
        settings_for_run(settings, {"decision_memory_mode": "blackboard_v1"}).decision_memory_mode
        == "blackboard_v1"
    )
    with pytest.raises(ValueError):
        settings_for_run(settings, {"decision_memory_mode": "unregistered"})


def test_old_blocked_new_recorded_condition_and_same_url_negative_remain_claims():
    op = operation(0)
    condition_id = str(uuid4())
    op["context"]["source_condition_snapshots"] = {condition_id: {"conditions": {"K": "missing"}}}
    data = MemorySnapshot(
        boundary={"session_id": "s"},
        operations=(op,),
        conditions={condition_id: {"conditions": {"K": "new model claim with evidence"}}},
        condition_refs={condition_id: f"finding:{condition_id}@2"},
        claims=tuple(
            {
                "record_ref": f"attempt:{uuid4()}@3",
                "origin_kind": "model_claim",
                "statement": {"outcome": value, "action": "检查同一入口"},
                "source_refs": [op["record_ref"]],
            }
            for value in ("blocked", "negative")
        ),
    )
    board, _ = select_memory(request(explicit_refs=(op["record_ref"],)), data)
    assert {c.statement["outcome"] for c in board.previous_attempts} == {"blocked", "negative"}
    difference = next(
        d
        for d in board.condition_differences[op["record_ref"]]
        if d.field.startswith("condition_source:")
    )
    assert difference.status == "changed" and difference.old_value != difference.new_value
    assert f"finding:{condition_id}@2" in difference.source_refs
    assert board.retrieval_status["equivalence"] == "not_determined"
    assert "next_action" not in board.model_dump()


def test_explicit_missing_source_and_invalid_focus_are_visible_not_fabricated():
    op = operation(0, step="finished")
    missing = f"artifact:{uuid4()}"
    req = replace(
        request(focus_freshness="initial_input", explicit_refs=(missing,)),
        state={
            "current_focus": {"step_id": "finished"},
            "plan": [{"step_id": "finished", "status": "done"}],
        },
    )
    board, trace = select_memory(
        req, MemorySnapshot(boundary={"session_id": "s"}, operations=(op,))
    )
    assert not board.current_focus["focus_used_for_filtering"]
    assert board.retrieval_status["status"] == "not_retrieved"
    assert board.retrieval_status["source_count"] == 1 and board.retrieval_status[
        "unavailable_source_refs"
    ] == [missing]
    assert trace["candidates"][0]["selected"] is False


def test_unique_result_reference_groups_without_rewriting_original_owner():
    op = operation(0)
    artifact = {
        "record_ref": f"artifact:{uuid4()}",
        "source_operation_ref": None,
        "availability": "available",
    }
    op["result_artifact_refs"] = [artifact["record_ref"]]
    board, _ = select_memory(
        request(explicit_refs=(artifact["record_ref"],)),
        MemorySnapshot(boundary={"session_id": "s"}, operations=(op,), artifacts=(artifact,)),
    )
    assert (
        len(board.relevant_evidence) == 1
        and board.relevant_evidence[0].record_ref == op["record_ref"]
    )
    projected = next(
        a for a in board.evidence_references if a["record_ref"] == artifact["record_ref"]
    )
    assert projected["source_operation_ref"] is None
    assert (
        projected["evidence_group_ref"] == op["record_ref"]
        and "owner_unrecorded" in projected["association"]
    )
    assert "evidence_group_ref" not in artifact
