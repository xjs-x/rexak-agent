"""完成合同、旧版本兼容与焦点关联的确定性边界。"""

from __future__ import annotations

import pytest

from agent_workbench.agent.focus import assessment_applies
from agent_workbench.agent.state_migration import adapt_v5
from agent_workbench.contracts.planning import (
    HttpWorkItem,
    StepCompletionSpec,
    completion_review_reason,
)
from agent_workbench.tools.planning import normalize_steps


def test_legacy_adapter_marks_old_done_without_fabricating_assessment():
    original = {
        "state_schema_version": 5,
        "plan": [{"step_id": "A", "description": "历史工作", "status": "done"}],
        "work_assessment": {"step_id": "A", "changed": "历史判断"},
    }
    update = adapt_v5(original, checkpoint_id="original", next_nodes=("tools",))
    assert update["state_schema_version"] == 6
    assert update["plan"][0]["legacy_completion"]
    assert update["work_assessment"]["evaluated_step_ids"] == []
    assert update["work_assessment"]["evaluated_plan_revision"] is None
    assert "legacy_completion" not in original["plan"][0]
    assert completion_review_reason(update["plan"][0])
    assert "completion_spec" not in update["plan"][0]


def test_assessed_old_step_and_next_focus_are_independent():
    focus = {
        "step_id": "C",
        "lead_id": None,
        "intent": "补条件",
        "reason": "修复",
        "source": "explicit",
    }
    state = {
        "plan_revision": 3,
        "current_focus": focus,
        "work_assessment": {
            "step_id": "A",
            "lead_id": None,
            "evaluated_step_ids": ["A"],
            "evaluated_plan_revision": 2,
            "resulting_plan_revision": 3,
            "next_focus": focus,
        },
    }
    assert assessment_applies(state)
    assert not assessment_applies({**state, "plan_revision": 4})
    assert not assessment_applies({**state, "current_focus": {**focus, "step_id": "B"}})


@pytest.mark.parametrize(
    "extra",
    [
        {"check_type": "predicate"},
        {"check_type": "verification"},
        {"check_type": "model", "verification_id": "00000000-0000-0000-0000-000000000001"},
    ],
)
def test_check_method_requires_exact_parameters(extra):
    with pytest.raises(ValueError):
        StepCompletionSpec.model_validate({"mode": "investigate", "done_when": "明确结果", **extra})


def test_dependency_and_program_check_fields_are_not_silently_accepted():
    for extra in ({"depends_on": ["A"]}, {"completion_protocol": 1}, {"legacy_completion": False}):
        with pytest.raises(ValueError, match="Extra inputs"):
            normalize_steps([{"step_id": "A", "description": "工作", **extra}], [], None)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@example.test/x",
        "http://example.test/x?payload=secret",
        "http://example.test/x#fragment",
    ],
)
def test_http_work_scope_does_not_store_credentials_or_payload(url):
    with pytest.raises(ValueError):
        HttpWorkItem(method="get", url=url)


def test_loaded_version_changes_with_loaded_planning_checks(monkeypatch):
    from agent_workbench.agent.factory import loaded_code_version
    from agent_workbench.tools import planning

    original = loaded_code_version()
    monkeypatch.setattr(planning, "normalize_steps", lambda *_args, **_kwargs: [])
    assert loaded_code_version() != original
