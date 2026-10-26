"""最新真实快照与可见事实差额；不执行Agent或远端操作。"""

import copy
import json
from dataclasses import replace
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_workbench.agent.history_assistance import memory_view
from agent_workbench.contracts.decision_memory import DecisionInput, MemorySnapshot
from agent_workbench.domain.decision_memory import select_memory
from agent_workbench.domain.passive_reflection import encoded


def latest_requests():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/memory_second_live_s045.json").read_text(
            encoding="utf-8"
        )
    )
    snap = MemorySnapshot(**fixture["snapshot"])
    messages = {}
    for item in fixture["messages"]:
        if item["type"] == "tool":
            m = ToolMessage(
                content=encoded(item["content"]),
                id=item["id"],
                name=item["name"],
                tool_call_id=item["tool_call_id"],
            )
        elif item["type"] == "ai":
            m = AIMessage(content="", id=item["id"], tool_calls=item["calls"])
        else:
            m = HumanMessage(content=item["content"], id=item["id"])
        messages[m.id] = m
    for request in fixture["requests"]:
        ops = tuple(op for op in snap.operations if op["created_at"] < request["at"])
        refs = {op["record_ref"] for op in ops}
        data = replace(
            snap,
            operations=ops,
            artifacts=tuple(a for a in snap.artifacts if a["source_operation_ref"] in refs),
        )
        query = DecisionInput(
            session_id=snap.boundary["session_id"],
            run_id=fixture["source_run"],
            control_version=0,
            state={"plan_revision": 0},
            executor_id="kali-01",
            focus_freshness="initial_input",
        )
        yield (
            select_memory(query, data)[0],
            [messages[key] for key in request["ids"]],
            request,
            fixture["source_run"],
        )


def test_latest_seven_visible_references_need_no_memory_restatement():
    rows = list(latest_requests())
    assert sum(r[2]["before_bytes"] for r in rows) == 16670
    assert len({ref for row in rows for ref in row[2]["before_refs"]}) == 7
    for index, (board, messages, _, run) in enumerate(rows):
        saved = copy.deepcopy(messages)
        trace = {}
        assert memory_view(board, run, 3, 16384, set(), messages=messages, trace=trace) is None
        assert messages == saved
        assert trace["status"] == ("already_visible" if index else "no_relevant_history")
        assert trace["projection_bytes"] == 0


def test_trimmed_or_partial_latest_receipt_returns_history_without_claiming_body_read():
    board, messages, _, run = list(latest_requests())[1]
    result = memory_view(board, run, 3, 16384, set(), messages=[])
    assert result and all(
        v["evidence_availability"] == "metadata_only_no_body_read"
        for v in result["execution_evidence"]
    )
    first = next(m for m in messages if isinstance(m, ToolMessage))
    data = json.loads(str(first.content))
    data["truncated"] = True
    partial = [
        m.model_copy(update={"content": encoded(data)}) if m is first else m for m in messages
    ]
    result = memory_view(board, run, 3, 16384, set(), messages=partial)
    assert result and "operation:" + data["operation_id"] in {
        v["record_ref"] for v in result["execution_evidence"]
    }


def test_new_job_state_is_incremental_even_when_original_tool_receipt_visible():
    board, messages, _, run = list(latest_requests())[1]
    original = next(u for u in board.relevant_evidence if u.execution["jobs_last_observed"])
    execution = copy.deepcopy(original.execution)
    execution["jobs_last_observed"][0].update(
        last_observed_state="unknown", sync_status="uncertain"
    )
    changed = original.model_copy(update={"execution": execution})
    board = board.model_copy(update={"relevant_evidence": [changed]})
    result = memory_view(board, run, 3, 16384, set(), messages=messages)
    assert result
    assert (
        result["execution_evidence"][0]["execution"]["jobs_last_observed"][0]["last_observed_state"]
        == "unknown"
    )
    assert original.execution["jobs_last_observed"][0]["last_observed_state"] == "succeeded"


def test_no_history_visible_budget_and_error_are_distinct():
    empty, _, _, run = next(iter(latest_requests()))
    trace = {}
    assert memory_view(empty, run, 3, 16384, set(), trace=trace) is None
    assert trace["status"] == "no_relevant_history"
    board, _, _, run = list(latest_requests())[-1]
    assert memory_view(board, run, 3, 10, set(), trace=trace) is None
    assert trace["status"] == "omitted_by_budget" and trace["candidate_count"] > 0


async def test_manifest_distinguishes_retrieval_error_and_timeout(tmp_path, monkeypatch):
    from typing import Any, cast

    from agent_workbench.agent.middleware import history_assistance as module
    from tests.unit.test_history_assistance_mvp import Request, box_for

    for error, expected in [(TimeoutError(), "retrieval_timeout"), (OSError(), "retrieval_error")]:

        async def fail(*args, failure=error):
            raise failure

        monkeypatch.setattr(module, "build_blackboard", fail)
        request = Request(box_for(tmp_path))
        seen = []

        async def handler(value, calls=seen):
            calls.append(value)
            return object()

        await cast(Any, module.HistoryAssistanceMiddleware()).awrap_model_call(request, handler)
        info = request.runtime.context.toolbox.history_assistance_manifest
        assert info["memory"] == expected
        assert info["selection"]["candidate_count"] is None
        assert info["selected_refs"] == [] and info["bytes"] == 0
        assert len(seen) == 1


def test_reference_poll_timestamps_are_not_new_facts():
    board, _, _, run = list(latest_requests())[-1]
    previous = memory_view(board, run, 3, 16384, set())
    assert previous
    message = AIMessage(content=encoded({"_host_history_reference": {"memory": previous}}))
    units = []
    for unit in board.relevant_evidence:
        execution = copy.deepcopy(unit.execution)
        for job in execution.get("jobs_last_observed", []):
            job["last_synced_at"] = "2030-01-01"
        for resource in execution.get("resources_last_observed", []):
            resource["last_checked_at"] = "2030-01-01"
        units.append(unit.model_copy(update={"execution": execution}))
    assert (
        memory_view(
            board.model_copy(update={"relevant_evidence": units}),
            run,
            3,
            16384,
            set(),
            messages=[message],
        )
        is None
    )


async def test_projection_error_does_not_masquerade_as_retrieval_failure(tmp_path, monkeypatch):
    from typing import Any, cast

    from agent_workbench.agent.middleware import history_assistance as module
    from tests.unit.test_history_assistance_mvp import Request, board_for, box_for

    async def read(*args):
        return board_for(), {}

    def invalid_view(*args, **kwargs):
        raise ValueError("invalid optional projection")

    monkeypatch.setattr(module, "build_blackboard", read)
    monkeypatch.setattr(module, "memory_view", invalid_view)
    box = box_for(tmp_path)
    seen = []

    async def handler(request):
        seen.append(request)
        return object()

    await cast(Any, module.HistoryAssistanceMiddleware()).awrap_model_call(Request(box), handler)
    assert len(seen) == 1
    assert box.history_assistance_manifest["memory"] == "projection_error"
    assert "retrieval_error_type" not in box.history_assistance_manifest
    assert box.history_assistance_manifest["bytes"] == 0
