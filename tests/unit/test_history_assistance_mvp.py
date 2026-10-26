"""历史辅助的模式、低权限边界、预算回退和控制信号。"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.history_assistance import all_tools_paired, memory_view
from agent_workbench.agent.middleware import history_assistance as module
from agent_workbench.agent.middleware.history_assistance import (
    HistoryAssistanceMiddleware,
    reference_messages,
)
from agent_workbench.agent.signals import GraphRunAborted, LeaseLostError, StaleResponseError
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.config.settings import Settings, settings_for_run
from agent_workbench.contracts.decision_memory import (
    ConditionDifference,
    DecisionBlackboard,
    MemoryUnit,
)
from agent_workbench.contracts.tool import ExecutorCapabilities
from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.models.provider import ModelBinding
from agent_workbench.tools.registry import ToolSelection, build_tools, default_selection
from tests.support.scripted_model import ScriptedChatModel
from tests.unit.test_context_budget import profile


@pytest.mark.parametrize("mode", ["off", "memory", "passive"])
def test_run_mode_is_pinned_and_legacy_observer_cannot_override_new_off(mode):
    cfg = Settings(
        history_assistance_mode="passive",
        history_action_shadow_enabled=True,
        decision_memory_mode="blackboard_v1",
    )
    result = settings_for_run(cfg, {"history_assistance_mode": mode})
    assert result.history_assistance_mode == mode
    assert result.history_action_shadow_enabled is (mode == "passive")
    assert result.decision_memory_mode == "current"
    assert settings_for_run(cfg, {}).history_assistance_mode == "off"


def test_invalid_mode_and_environment_entry(monkeypatch):
    monkeypatch.setenv("WORKBENCH_HISTORY_ASSISTANCE_MODE", "memory")
    assert cast(Any, Settings)(_env_file=None).history_assistance_mode == "memory"
    with pytest.raises(ValueError):
        settings_for_run(Settings(), {"history_assistance_mode": "invalid"})
    with pytest.raises(ValidationError):
        Settings(history_assistance_mode=cast(Any, "invalid"))


def test_explicit_operational_rollback_overrides_existing_pinned_run():
    result = settings_for_run(
        Settings(history_assistance_force_off=True, history_action_shadow_enabled=True),
        {"history_assistance_mode": "passive"},
    )
    assert result.history_assistance_mode == "off"
    assert result.decision_memory_mode == "current"
    assert not result.history_action_shadow_enabled


def test_production_assembly_off_has_no_new_middleware_and_passive_precedes_observer():
    for mode in ("off", "memory", "passive"):
        cfg = Settings(history_assistance_mode=cast(Any, mode))
        names = [
            type(m).__name__
            for m in build_agent_middleware(
                ScriptedChatModel(), ModelBinding(profile()), cfg, [], default_price_catalog()
            )
        ]
        assert ("HistoryAssistanceMiddleware" in names) is (mode != "off")
        if mode != "off":
            assert (
                names.index("ContextBuilderMiddleware")
                < names.index("HistoryAssistanceMiddleware")
                < names.index("ModelObserverMiddleware")
            )


def test_passive_does_not_open_recipe_without_explicit_opt_in_and_capability(tmp_path):
    caps = ExecutorCapabilities(
        executor_id="fake",
        service_version="fixture",
        protocol_version=1,
        boot_id="boot",
        started_at=datetime.now(UTC),
    )
    base = Settings(
        history_assistance_mode="passive",
        history_action_shadow_context_path=tmp_path / "policy.json",
    )
    assert "run_http_test_profile" not in {
        t.name for t in build_tools(default_selection(base, capabilities=caps))
    }
    opted = base.model_copy(update={"history_action_test_profiles_enabled": True})
    assert "run_http_test_profile" in {
        t.name for t in build_tools(default_selection(opted, capabilities=caps))
    }
    assert "run_http_test_profile" not in {t.name for t in build_tools(default_selection(opted))}
    assert "run_http_test_profile" not in {
        t.name for t in build_tools(ToolSelection(http=False, http_test_profiles=True))
    }
    no_http = caps.model_copy(update={"http_available": False})
    assert not default_selection(opted, capabilities=no_http).http_test_profiles


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
        "tools/history_feedback.py",
        "domain/passive_reflection.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "from experiments" not in source and "import experiments" not in source


async def test_plain_navigation_does_not_allocate_shadow_service(tmp_path):
    from experiments.history_action_autonomous_shadow_v1.fixture import make_box

    from agent_workbench.tools.history_action_shadow import enqueue_http

    box = make_box(tmp_path)
    box.settings.history_assistance_mode = "passive"
    try:
        enqueue_http(box, {}, {"operation_id": "navigation"}, "nav", None)
        assert box.history_action_shadow is None
    finally:
        await box.executor.close()


def test_feedback_receipt_survives_another_python_process(tmp_path):
    import subprocess
    import sys
    from uuid import uuid4

    from agent_workbench.domain.history_action_reflection import digest
    from agent_workbench.tools.history_feedback import claim_reference

    run, session = str(uuid4()), str(uuid4())
    value = {"material_key": "same-trusted-material"}
    key = digest({"run": run, "thread": "thread", "material": value["material_key"]})
    path = tmp_path / "history_action_shadow" / session / "feedback" / (key + ".json")
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_workbench.config.settings import Settings
from agent_workbench.tools.history_feedback import claim_reference
box=SimpleNamespace(run_id=sys.argv[2],session_id=sys.argv[3],settings=Settings(_env_file=None,data_root=Path(sys.argv[1])))
assert claim_reference(box,'thread',{'material_key':'same-trusted-material'},Path(sys.argv[4]))
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), run, session, str(path)],
        check=True,
        timeout=20,
    )
    box = SimpleNamespace(run_id=run, session_id=session, settings=Settings(data_root=tmp_path))
    assert not claim_reference(box, "thread", value, path)


def test_shadow_uses_actual_decision_view_not_archived_checkpoint_body(tmp_path):
    from agent_workbench.tools.history_action_shadow import dispatch_context, visible_context

    original = paired()
    original[-1] = original[-1].model_copy(update={"content": "BODY_NOT_SENT_TO_MODEL"})
    sent = [*original[:-1], original[-1].model_copy(update={"content": "正文已归档，只有引用"})]
    current = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "HIDDEN_NOT_CAPTURED"},
        tool_calls=[{"name": "test", "args": {}, "id": "next", "type": "tool_call"}],
    )
    box = box_for(tmp_path, "passive")
    box.history_assistance_visible_context = visible_context(sent, "thread", 262144)
    runtime = SimpleNamespace(
        context=SimpleNamespace(toolbox=box, thread_id="thread"),
        state={"messages": [*original, current]},
        tool_call_id="next",
    )
    captured = dispatch_context(runtime)
    serialized = json.dumps(captured, ensure_ascii=False)
    assert "正文已归档" in serialized and "BODY_NOT_SENT_TO_MODEL" not in serialized
    assert "HIDDEN_NOT_CAPTURED" not in serialized and captured["batch_ids"] == ["next"]
    box.history_assistance_visible_context = None
    with pytest.raises(ValueError, match="actual_decision_view_unavailable"):
        dispatch_context(runtime)


def test_feedback_external_bytes_are_strict_utf8_and_preserved_on_error(tmp_path):
    from agent_workbench.tools.history_feedback import read_bounded

    path = tmp_path / "external.json"
    raw = '{"value":"历史"}'.encode("utf-16")
    path.write_bytes(raw)
    with pytest.raises(UnicodeDecodeError):
        read_bounded(path, 1024)
    assert path.read_bytes() == raw
