"""第二版验收中发现的真实接缝缺口；全部使用合成数据。"""

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from workbench_executor.http_encoding import MultipartPart as ExecutorPart

from agent_workbench.agent.focus import resolve_focus
from agent_workbench.agent.middleware.stage_report import stage_metadata
from agent_workbench.agent.state import CurrentFocus
from agent_workbench.contracts.http import MultipartPart
from agent_workbench.domain.reports import _stage_coverage
from agent_workbench.models.context_budget import estimate_request
from agent_workbench.models.response_quality import batch_rejection, classify_response
from tests.unit.test_context_budget import profile


def test_request_settings_output_limit_is_used_by_actual_budget_path():
    estimate = estimate_request(
        [HumanMessage(content="synthetic")],
        None,
        [],
        profile(50000, 1000),
        model_settings={"max_tokens": 16000},
    )
    assert estimate.output_limit == 16000
    assert estimate.allowed_input_tokens == int((50000 - 16000) * 0.9)


def test_explicit_focus_and_return_point_survive_automatic_refresh():
    plan = [
        {"step_id": "main", "description": "main", "status": "pending"},
        {"step_id": "probe", "description": "probe", "status": "in_progress"},
    ]
    explicit = cast(
        CurrentFocus,
        {
            "step_id": "probe",
            "lead_id": "lead-probe",
            "intent": "discriminate",
            "reason": "new evidence",
            "source": "explicit",
            "return_to_step_id": "main",
        },
    )
    focus, _ = resolve_focus(plan, explicit, None)
    assert focus == explicit


@pytest.mark.parametrize("status", ["done", "dropped", "blocked"])
def test_explicit_focus_cannot_select_a_closed_or_blocked_step(status):
    with pytest.raises(ValueError):
        resolve_focus(
            [{"step_id": "s", "status": status}],
            None,
            {"step_id": "s", "intent": "test", "reason": "synthetic"},
        )


def test_return_point_must_belong_to_plan():
    with pytest.raises(ValueError):
        resolve_focus(
            [{"step_id": "s", "status": "in_progress"}],
            None,
            {
                "step_id": "s",
                "intent": "test",
                "reason": "synthetic",
                "return_to_step_id": "missing",
            },
        )


@pytest.mark.parametrize("model", [MultipartPart, ExecutorPart])
def test_multipart_limit_is_utf8_bytes_on_both_sides(model: Any):
    with pytest.raises(ValueError):
        model(name="field", value="中" * 100000)


def test_blank_multipart_metadata_normalizes_identically():
    payload = {"name": "field", "value": "test", "filename": " ", "content_type": " "}
    assert MultipartPart(**payload).model_dump() == ExecutorPart(**payload).model_dump()


def test_old_assessment_cannot_be_current_next_action_after_switch():
    metadata = stage_metadata(
        {
            "current_focus": {"step_id": "new", "lead_id": None},
            "work_assessment": {"step_id": "old", "lead_id": None, "next_action": "old action"},
        },
        run_id="synthetic",
        trigger="test",
        signature="fixed",
    )
    assert metadata["assessment_applies_to_focus"] is False
    assert metadata["next_action"] is None


def test_report_detects_revision_changes_and_ignores_row_order():
    metadata = {
        "covered_position": {
            "domain_versions": {
                "observations": {"a": 1, "z": 1},
                "findings": {"f": 2},
                "leads": {"x": 1},
            }
        }
    }
    unchanged = _stage_coverage(
        metadata,
        observations_last="a",
        findings_last="f",
        leads_last="x",
        domain_versions={"observations": {"z": 1, "a": 1}, "findings": {"f": 2}, "leads": {"x": 1}},
    )
    assert not unchanged["newer_domain_records_present"]
    changed = _stage_coverage(
        metadata,
        observations_last="a",
        findings_last="f",
        leads_last="x",
        domain_versions={"observations": {"a": 1, "z": 1}, "findings": {"f": 3}, "leads": {"x": 1}},
    )
    assert changed["newer_domains"] == ["findings"]


def test_duplicate_tool_ids_and_whitespace_reply_are_unusable():
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "probe", "args": {}, "id": "same"},
            {"name": "probe", "args": {}, "id": "same"},
        ],
    )
    assert batch_rejection(message) is not None
    assert classify_response(AIMessage(content=" \n\t")).outcome == "empty"
