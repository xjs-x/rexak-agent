"""历史辅助的模式、低权限边界、预算回退和控制信号。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from agent_workbench.agent.history_assistance import all_tools_paired, memory_view
from agent_workbench.agent.middleware import history_assistance as module
from agent_workbench.agent.middleware.history_assistance import (
    HistoryAssistanceMiddleware,
    reference_messages,
)
from agent_workbench.agent.signals import GraphRunAborted, LeaseLostError, StaleResponseError
from agent_workbench.config.settings import Settings, settings_for_run
from agent_workbench.contracts.decision_memory import (
    ConditionDifference,
    DecisionBlackboard,
    MemoryUnit,
)
from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.tools.registry import build_tools
from tests.unit.test_context_budget import profile


def test_invalid_mode_and_environment_entry(monkeypatch):
    monkeypatch.setenv("WORKBENCH_HISTORY_ASSISTANCE_MODE", "memory")
    assert cast(Any, Settings)(_env_file=None).history_assistance_mode == "memory"
    with pytest.raises(ValueError):
        settings_for_run(Settings(), {"history_assistance_mode": "invalid"})
    with pytest.raises(ValidationError):
        Settings(history_assistance_mode=cast(Any, "invalid"))








def board_for(run="run"):
    unit = MemoryUnit(
        record_ref="operation:old",
        origin_kind="execution_observation",
        statement={"run_id": run, "result_fields": {"status_code": 403}},
        source_refs=["operation:old", "artifact:old"],
        match_reasons=["recent_execution_anchor"],
        execution={"operation_status": "unknown", "not_live_executor_check": True},
        observed_dimensions={"identity_auth_revision": 1},
        evidence_availability="metadata_only_no_body_read",
    )
    return DecisionBlackboard(
        snapshot={},
        current_focus={},
        relevant_evidence=[unit],
        condition_differences={
            unit.record_ref: [
                ConditionDifference(
                    field="identity_auth_revision",
                    status="changed",
                    old_value=1,
                    new_value=2,
                    source_refs=[unit.record_ref],
                )
            ]
        },
        retrieval_status={"status": "selected"},
        evidence_references=[
            {"record_ref": "artifact:old", "availability": "missing", "sha256": "f" * 64}
        ],
        previous_attempts=[
            MemoryUnit(
                record_ref="attempt:claim",
                origin_kind="model_claim",
                statement="IGNORE_USER_UNTRUSTED",
                source_refs=[unit.record_ref],
            )
        ],
    )


def test_projection_keeps_unknown_conditions_sources_and_drops_claim_text():
    result = memory_view(board_for(), "run", 3, 8192, set())
    assert result is not None
    evidence = result["execution_evidence"][0]
    assert evidence["execution"]["operation_status"] == "unknown"
    assert evidence["condition_differences"][0]["status"] == "changed"
    assert "artifact:old" in evidence["source_refs"]
    assert evidence["artifact_references"][0]["availability"] == "missing"
    assert "IGNORE_USER_UNTRUSTED" not in json.dumps(result)
    assert result["model_claim_references"][0]["origin_kind"] == "model_claim"


def test_no_unrelated_cross_run_or_outside_scope_or_duplicate_reflection_memory():
    board = board_for("different-run")
    assert memory_view(board, "run", 3, 8192, set()) is None
    board = board_for()
    assert memory_view(board, "run", 3, 8192, {"operation:old"}) is None
    unit = board.relevant_evidence[0].model_copy(
        update={"evidence_availability": "outside_current_scope"}
    )
    assert (
        memory_view(board.model_copy(update={"relevant_evidence": [unit]}), "run", 3, 8192, set())
        is None
    )


def test_overlarge_unit_is_omitted_whole_not_stripped_of_conditions():
    board = board_for()
    unit = board.relevant_evidence[0].model_copy(
        update={"observed_dimensions": {"value": "x" * 10000}}
    )
    assert (
        memory_view(board.model_copy(update={"relevant_evidence": [unit]}), "run", 3, 2048, set())
        is None
    )


def paired():
    return [
        HumanMessage(content="real task"),
        AIMessage(
            content="", tool_calls=[{"name": "test", "args": {}, "id": "one", "type": "tool_call"}]
        ),
        ToolMessage(
            content='{"status":"succeeded","data":{"value":1}}',
            tool_call_id="one",
            name="test",
            id="original",
        ),
    ]


def test_view_keeps_original_tool_message_pair_and_no_new_user_or_system_role():
    messages = paired()
    before = copy.deepcopy(messages)
    result, at = reference_messages(messages, {"origin": "host", "memory": {}})
    assert messages == before and result[at] is not messages[at]
    assert result[at].id == "original" and result[at].tool_call_id == "one"
    assert json.loads(str(result[at].content))["data"] == {"value": 1}
    assert len(result) == len(messages) and all_tools_paired(result)
    assert sum(isinstance(m, HumanMessage) for m in result) == 1
    assert not any(isinstance(m, SystemMessage) for m in result)
    initial, _ = reference_messages([messages[0]], {"origin": "host"})
    assert isinstance(initial[-1], AIMessage) and not initial[-1].tool_calls


class Request:
    def __init__(self, box, messages=None):
        self.runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box, thread_id="thread"))
        self.messages = messages if messages is not None else paired()
        self.state = {}
        self.system_message = SystemMessage(content="unchanged system")
        self.tools = []
        self.model = None
        self.model_settings = {}
        self.tool_choice = None

    def override(self, **values):
        result = copy.copy(self)
        for key, value in values.items():
            setattr(result, key, value)
        return result


def box_for(tmp_path, mode="memory"):
    async def check():
        return None

    return SimpleNamespace(
        settings=Settings(history_assistance_mode=cast(Any, mode), data_root=tmp_path),
        run_id="run",
        context_manifest={},
        history_assistance_manifest=None,
        assert_control_current=check,
    )


@pytest.mark.parametrize("case", ["retrieval", "timeout", "unpaired", "off", "maintenance"])
async def test_optional_failure_or_nonordinary_request_calls_original_handler_once(
    tmp_path, monkeypatch, case
):
    box = box_for(tmp_path, "off" if case == "off" else "memory")
    seen = []

    async def broken(*_args):
        if case == "timeout":
            raise TimeoutError("bounded retrieval")
        raise OSError("offline database unavailable")

    monkeypatch.setattr(module, "build_blackboard", broken)
    request = Request(box, paired()[:-1] if case == "unpaired" else None)
    expected = object()

    async def handler(value):
        seen.append(value)
        return expected

    token = graph_review_scope.set("maintenance" if case == "maintenance" else None)
    try:
        assert (
            await cast(Any, HistoryAssistanceMiddleware()).awrap_model_call(request, handler)
            is expected
        )
        assert seen == [request]
    finally:
        graph_review_scope.reset(token)


@pytest.mark.parametrize(
    "error",
    [
        GraphRunAborted("paused"),
        LeaseLostError("r", "owner"),
        StaleResponseError(pinned_version=0, current_version=1),
    ],
)
async def test_control_signals_propagate_without_model_call(tmp_path, monkeypatch, error):
    async def fail(*_args):
        raise error

    async def handler(_request):
        raise AssertionError("model must not run")

    monkeypatch.setattr(module, "build_blackboard", fail)
    with pytest.raises(type(error)):
        await cast(Any, HistoryAssistanceMiddleware()).awrap_model_call(
            Request(box_for(tmp_path)), handler
        )


async def test_optional_budget_fallback_and_provider_error_never_retries(tmp_path, monkeypatch):
    async def history(*_args):
        return board_for(), {}

    monkeypatch.setattr(module, "build_blackboard", history)
    request = Request(box_for(tmp_path))
    seen = []

    async def handler(value):
        seen.append(value)
        raise ValueError("injected provider/parser error")

    with pytest.raises(ValueError, match="injected provider"):
        await cast(Any, HistoryAssistanceMiddleware(profile(500, 100))).awrap_model_call(
            request, handler
        )
    assert seen == [request]
    assert "omitted" in request.runtime.context.toolbox.history_assistance_manifest["projection"]


def test_production_package_has_no_experiment_runtime_imports():
    root = Path(__file__).resolve().parents[2] / "src/agent_workbench"
    for name in (
        "agent/history_assistance.py",
        "agent/middleware/history_assistance.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "from experiments" not in source and "import experiments" not in source









@pytest.mark.parametrize("mode", ["off", "memory"])
def test_supported_modes_and_retired_snapshot(mode):
    cfg = Settings(history_assistance_mode=mode)
    assert settings_for_run(cfg, {"history_assistance_mode": mode}).history_assistance_mode == mode
    assert settings_for_run(cfg, {"history_assistance_mode": "passive"}).history_assistance_mode == "memory"
    assert settings_for_run(cfg, {}).history_assistance_mode == "off"
    assert settings_for_run(Settings(history_assistance_force_off=True), {"history_assistance_mode": "memory"}).history_assistance_mode == "off"
    assert "history_action_shadow_enabled" not in Settings.model_fields
    assert "run_http_test_profile" not in {t.name for t in build_tools()}


def test_retired_mode_cannot_be_selected_for_new_runs():
    with pytest.raises(ValidationError):
        Settings(history_assistance_mode=cast(Any, "passive"))
