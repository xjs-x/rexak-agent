"""真实六操作脱敏快照的局部回归；不执行模型、工具或外部请求。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_workbench.agent.history_assistance import encoded, memory_view
from agent_workbench.contracts.decision_memory import DecisionInput, MemorySnapshot
from agent_workbench.domain.decision_memory import select_memory

FIXTURE = Path(__file__).parents[1] / "fixtures" / "memory_live_s045.json"
RUN = "eb96c527-9465-4413-89e0-2db49b213f13"
SUCCESS = "operation:64b83762-24f9-4b9e-bfb0-f8d1d109b782"
UNKNOWN = {
    "operation:cd9b766f-738a-4180-830c-782c81839d7d",
    "operation:8cd58e5e-3f5d-4aa8-915c-49a3d66fd612",
}


def snapshot():
    return MemorySnapshot(**json.loads(FIXTURE.read_text(encoding="utf-8"))["snapshot"])


def board(data=None):
    data = data or snapshot()
    request = DecisionInput(
        session_id=data.boundary["session_id"],
        run_id=RUN,
        control_version=0,
        state={},
        executor_id="kali-01",
        focus_freshness="initial_input",
    )
    return select_memory(request, data)[0]


def replay():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    messages = {}
    for item in fixture["messages"]:
        if item["type"] == "tool":
            message = ToolMessage(
                content=encoded(item["content"]),
                id=item["id"],
                name=item["name"],
                tool_call_id=item["tool_call_id"],
            )
        elif item["type"] == "ai":
            message = AIMessage(content="", id=item["id"], tool_calls=item["calls"])
        else:
            message = HumanMessage(content=item["content"], id=item["id"])
        messages[message.id] = message
    data = snapshot()
    for request in fixture["requests"]:
        ops = tuple(op for op in data.operations if op["created_at"] < request["at"])
        refs = {op["record_ref"] for op in ops}
        current = replace(
            data,
            operations=ops,
            artifacts=tuple(a for a in data.artifacts if a["source_operation_ref"] in refs),
        )
        yield board(current), [messages[key] for key in request["ids"]]


def test_live_snapshot_latest_success_and_both_unknown_fit_together():
    result = memory_view(board(), RUN, 3, 16 * 1024, set())
    assert result is not None
    assert {v["record_ref"] for v in result["execution_evidence"]} == UNKNOWN | {SUCCESS}
    assert result["execution_evidence"][0]["record_ref"] == SUCCESS
    for item in result["execution_evidence"]:
        assert item["source_refs"] and item["execution"]["not_live_executor_check"]
        if item["record_ref"] in UNKNOWN:
            assert item["execution"]["operation_status"] == "unknown"
            assert item["execution"]["jobs_last_observed"][0]["sync_status"] == "uncertain"
    assert len(encoded(result).encode()) < 16 * 1024


def test_older_history_and_http_method_do_not_determine_priority():
    data = snapshot()
    older = [
        dict(
            data.operations[0],
            record_ref=f"operation:older-{i}",
            created_at="2020-01-01",
            updated_at="2020-01-01",
        )
        for i in range(30)
    ]
    changed = [dict(op, dimensions={**op["dimensions"], "method": "GET"}) for op in data.operations]
    result = memory_view(
        board(replace(data, operations=tuple(older + changed))), RUN, 3, 16384, set()
    )
    assert result is not None
    assert {v["record_ref"] for v in result["execution_evidence"]} == UNKNOWN | {SUCCESS}


def test_new_artifact_reactivates_an_older_operation_outside_recent_creation_window():
    data = snapshot()
    older = data.operations[0]
    fresh = {
        **data.artifacts[0],
        "record_ref": "artifact:new-capture",
        "source_operation_ref": older["record_ref"],
        "captured_at": "2030-01-01 00:00:00+00:00",
    }
    result = memory_view(
        board(replace(data, artifacts=(*data.artifacts, fresh))), RUN, 3, 16384, set()
    )
    assert result is not None
    assert result["execution_evidence"][0]["record_ref"] == older["record_ref"]
    assert "artifact:new-capture" in result["execution_evidence"][0]["source_refs"]


def test_recent_unknown_resolution_is_prioritized_without_rewriting_old_state():
    data = snapshot()
    op = data.operations[3]
    resolved = {
        **op,
        "operation_status": "failed",
        "updated_at": "2030-01-01",
        "jobs": [
            {**j, "last_observed_state": "failed", "sync_status": "in_sync"} for j in op["jobs"]
        ],
    }
    changed = replace(data, operations=tuple(resolved if x is op else x for x in data.operations))
    result = memory_view(board(changed), RUN, 3, 16384, set())
    assert result is not None
    assert result["execution_evidence"][0]["record_ref"] == op["record_ref"]
    assert result["execution_evidence"][0]["execution"]["operation_status"] == "failed"
    assert op["operation_status"] == "unknown"


def test_live_request_replay_reduces_bytes_without_losing_unresolved_results():
    sizes, selections = [], []
    for current, messages in replay():
        result = memory_view(current, RUN, 3, 16384, set(), messages=messages)
        sizes.append(len(encoded(result).encode()) if result else 0)
        selections.append(
            {v["record_ref"] for v in result["execution_evidence"]} if result else set()
        )
    assert sizes[0] == 0
    # unknown仍在当前原始工具回执中，无需用Memory再复制；裁剪后的召回另有覆盖。
    for _, messages in list(replay())[-3:]:
        visible_unknown = {
            "operation:" + d["operation_id"]
            for m in messages
            if isinstance(m, ToolMessage)
            for d in [json.loads(str(m.content))]
            if d.get("status") == "unknown"
        }
        assert visible_unknown >= UNKNOWN
    # 旧运行的7次参考共41194字节；新参考展示更多关键状态，仍至少减少四分之一。
    assert sum(sizes) < 41194 * 0.75, sizes


def test_current_visible_reference_is_incremental_and_returns_after_trimming():
    original = board()
    result = memory_view(original, RUN, 3, 16384, set())
    assert result is not None
    message = AIMessage(content=encoded({"_host_history_reference": {"memory": result}}))
    trace = {}
    assert memory_view(original, RUN, 3, 16384, set(), messages=[message], trace=trace) is None
    assert all(v["reason"] == "unchanged_reference_visible" for v in trace["omitted"])
    assert memory_view(original, RUN, 3, 16384, set(), messages=[]) == result


def test_changed_status_or_new_artifact_is_not_hidden_by_old_reference():
    old = board()
    previous = memory_view(old, RUN, 3, 16384, set())
    assert previous is not None
    message = AIMessage(content=encoded({"_host_history_reference": {"memory": previous}}))
    target = next(v for v in old.relevant_evidence if v.record_ref in UNKNOWN)
    changed = target.model_copy(
        update={
            "execution": {**target.execution, "operation_status": "succeeded"},
            "source_refs": [*target.source_refs, "artifact:new"],
        }
    )
    updated = old.model_copy(
        update={
            "relevant_evidence": [changed],
            "evidence_references": [
                *old.evidence_references,
                {"record_ref": "artifact:new", "availability": "available", "sha256": "a" * 64},
            ],
        }
    )
    result = memory_view(updated, RUN, 3, 16384, set(), messages=[message])
    assert result is not None
    assert result["execution_evidence"][0]["execution"]["operation_status"] == "succeeded"
    assert result["execution_evidence"][0]["artifact_references"][0]["record_ref"] == "artifact:new"


def test_overlarge_candidate_does_not_block_later_candidate_with_limit_one():
    original = board()
    first = original.relevant_evidence[0].model_copy(
        update={"observed_dimensions": {"large": "x" * 20000}}
    )
    rest = original.relevant_evidence[1]
    result = memory_view(
        original.model_copy(update={"relevant_evidence": [first, rest]}), RUN, 1, 6000, set()
    )
    assert result is not None
    assert result["execution_evidence"][0]["record_ref"] == rest.record_ref


def test_changed_condition_and_new_artifact_survive_visible_original_tool_result():
    current, messages = list(replay())[-1]
    result = memory_view(current, RUN, 3, 16384, set(), messages=messages)
    refs = {v["record_ref"] for v in result["execution_evidence"]} if result else set()
    # 已处理且原结果仍可见的旧成功无需复述；裁剪后必须能够再召回。
    assert SUCCESS not in refs
    restored = memory_view(current, RUN, 3, 16384, set(), messages=[])
    assert restored is not None and SUCCESS in {
        v["record_ref"] for v in restored["execution_evidence"]
    }


def test_changed_condition_is_not_deduplicated_against_visible_success():
    from agent_workbench.contracts.decision_memory import ConditionDifference

    current, messages = list(replay())[-1]
    changed = ConditionDifference(
        field="authentication",
        status="changed",
        old_value="anonymous",
        new_value="different_identity",
        source_refs=[SUCCESS],
    )
    current = current.model_copy(
        update={"condition_differences": {**current.condition_differences, SUCCESS: [changed]}}
    )
    result = memory_view(current, RUN, 3, 16384, set(), messages=messages)
    assert result is not None
    entry = next(v for v in result["execution_evidence"] if v["record_ref"] == SUCCESS)
    assert entry["condition_differences"][0]["status"] == "changed"


def test_missing_identity_conditions_are_supplemented_even_with_visible_original():
    current, messages = list(replay())[-1]
    evidence = [
        unit.model_copy(
            update={
                "observed_dimensions": {
                    **unit.observed_dimensions,
                    "identity_id": "old-identity",
                    "authentication": "session_identity",
                }
            }
        )
        if unit.record_ref == SUCCESS
        else unit
        for unit in current.relevant_evidence
    ]
    result = memory_view(
        current.model_copy(update={"relevant_evidence": evidence}),
        RUN,
        3,
        16384,
        set(),
        messages=messages,
    )
    assert result is not None
    entry = next(v for v in result["execution_evidence"] if v["record_ref"] == SUCCESS)
    assert entry["observed_dimensions"]["identity_id"] == "old-identity"


async def test_remaining_context_budget_reduces_projection_without_extra_model_call(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from agent_workbench.agent.middleware import history_assistance as module
    from agent_workbench.models.context_budget import RequestBudgetEstimate
    from tests.unit.test_history_assistance_mvp import Request, box_for

    current, messages = list(replay())[-2]
    messages = [m for m in messages if isinstance(m, HumanMessage)]
    box = box_for(tmp_path)
    box.run_id = RUN
    request = Request(box, messages)
    base_bytes = sum(len(str(m.content).encode()) for m in messages)

    async def read(*args):
        return current, {}

    def estimate(view, *args, **kwargs):
        added = sum(len(str(m.content).encode()) for m in view) - base_bytes
        return RequestBudgetEstimate(
            estimated_input_tokens=1000 + max(0, added) // 3,
            allowed_input_tokens=3500,
            components={},
            safety_margin_ratio=0.1,
        )

    monkeypatch.setattr(module, "build_blackboard", read)
    monkeypatch.setattr(module, "estimate_request", estimate)
    seen = []

    async def handler(outgoing):
        seen.append(outgoing)
        assert estimate(outgoing.messages).estimated_input_tokens <= 3500
        return SimpleNamespace()

    from tests.unit.test_context_budget import profile

    await cast(Any, module.HistoryAssistanceMiddleware(profile=profile())).awrap_model_call(
        request, handler
    )
    assert len(seen) == 1
    assert 0 < len(box.history_assistance_manifest["selected_refs"]) < 3
    assert box.history_assistance_manifest["selection"]["projection_bytes"] == box.history_assistance_manifest["memory_bytes"]
    assert messages == request.messages
