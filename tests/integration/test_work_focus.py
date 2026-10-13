"""焦点自动承接、轻量判断与同批快照关联。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_workbench.agent.focus import INFERRED_REASON, focus_signature, resolve_focus
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.agent.state import CurrentFocus
from agent_workbench.tools.execution_context import batch_tool_names, context_from_state
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


def _step(step_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"step_id": step_id, "description": f"步骤 {step_id}", "status": status, **extra}


def test_single_in_progress_step_is_adopted_without_faking_model_judgment():
    focus, hint = resolve_focus([_step("s1", "in_progress", lead_id="lead-1")], None, None)
    assert hint is None and focus is not None
    assert focus["step_id"] == "s1" and focus.get("source") == "plan_inferred"
    assert focus["reason"] == INFERRED_REASON
    assert focus["lead_id"] == "lead-1", "lead_id 只取该步骤已有绑定"


def test_single_in_progress_step_does_not_guess_lead_from_recent_tool():
    focus, _ = resolve_focus([_step("s1", "in_progress")], None, None)
    assert focus is not None and focus["lead_id"] is None


def test_multiple_in_progress_keeps_valid_previous_and_otherwise_hints():
    plan = [_step("s1", "in_progress"), _step("s2", "in_progress")]
    previous = cast(
        CurrentFocus,
        {
            "step_id": "s2",
            "lead_id": None,
            "intent": "核对条件",
            "reason": "模型选择",
            "return_to_step_id": None,
            "source": "explicit",
        },
    )
    kept, hint = resolve_focus(plan, previous, None)
    assert kept is not None and kept["step_id"] == "s2" and hint is None
    unresolved, hint2 = resolve_focus(plan, None, None)
    assert unresolved is None and hint2 and "明确" in hint2


def test_finished_or_dropped_step_is_not_kept_as_focus():
    previous = cast(
        CurrentFocus,
        {
            "step_id": "s1",
            "lead_id": "lead-1",
            "intent": "旧主线",
            "reason": "模型选择",
            "return_to_step_id": None,
            "source": "explicit",
        },
    )
    focus, hint = resolve_focus([_step("s1", "done")], previous, None)
    assert focus is None and hint is None, "完成的步骤不再作为当前焦点，也不强造新步骤"
    focus2, _ = resolve_focus(
        [_step("s1", "done"), _step("s2", "in_progress")], previous, None
    )
    assert focus2 is not None and focus2["step_id"] == "s2"


def test_explicit_focus_requires_existing_step_and_reason():
    with pytest.raises(ValueError, match="不属于当前计划"):
        resolve_focus([_step("s1", "pending")], None, {"step_id": "s9", "intent": "i", "reason": "r"})
    with pytest.raises(ValueError, match="简短理由"):
        resolve_focus([_step("s1", "pending")], None, {"step_id": "s1", "intent": "i"})
    focus, _ = resolve_focus(
        [_step("s1", "pending")],
        None,
        {"step_id": "s1", "intent": "验证条件", "reason": "唯一区分条件与路径问题"},
    )
    assert focus is not None and focus.get("source") == "explicit"


def test_focus_signature_ignores_identical_semantics():
    left = cast(CurrentFocus, resolve_focus([_step("s1", "in_progress")], None, None)[0])
    right = cast(CurrentFocus, dict(left))
    assert focus_signature(left) == focus_signature(right)
    assert focus_signature(left) != focus_signature(None)


async def test_middleware_adopts_focus_and_avoids_repeated_updates(database, unique_name):
    context = await context_for(database, unique_name)
    middleware = WorkingContextMiddleware()
    runtime = cast(Any, SimpleNamespace(context=context))
    state: dict[str, Any] = {
        "plan": [_step("s1", "in_progress")],
        "current_focus": None,
        "objective": "本地焦点测试",
    }
    update = await middleware.abefore_model(cast(Any, state), runtime)
    assert update is not None
    assert cast("dict[str, Any]", update["current_focus"])["source"] == "plan_inferred"
    state.update(update)
    again = await middleware.abefore_model(cast(Any, state), runtime)
    assert again is not None and "current_focus" not in again, "语义未变化时不反复写入"


async def test_middleware_clears_stale_focus_after_step_completes(database, unique_name):
    context = await context_for(database, unique_name)
    middleware = WorkingContextMiddleware()
    runtime = cast(Any, SimpleNamespace(context=context))
    state: dict[str, Any] = {
        "plan": [_step("s1", "done")],
        "current_focus": cast(
            CurrentFocus,
            {
                "step_id": "s1",
                "lead_id": None,
                "intent": "旧主线",
                "reason": "模型选择",
                "return_to_step_id": None,
                "source": "explicit",
            },
        ),
        "objective": "本地焦点测试",
    }
    update = await middleware.abefore_model(cast(Any, state), runtime)
    assert update is not None and update.get("current_focus") is None


def test_same_batch_plan_update_is_marked_as_original_snapshot():
    state = {
        "plan_revision": 4,
        "current_focus": {"step_id": "s1", "lead_id": None, "source": "explicit"},
        "messages": [
            HumanMessage(content="目标"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "update_plan", "args": {}, "id": "c1", "type": "tool_call"},
                    {"name": "http_request", "args": {}, "id": "c2", "type": "tool_call"},
                ],
            ),
        ],
    }
    names = batch_tool_names(state)
    assert names == ["update_plan", "http_request"]
    context = context_from_state(state, same_batch_plan_update=True)
    assert context["association"] == "focus_snapshot"
    assert context["plan_revision"] == 4
    assert context["focus_source"] == "explicit"
    assert "原快照" in context["association_note"]
    plain = context_from_state({"messages": []}, same_batch_plan_update=False)
    assert plain["association"] == "exploration"
