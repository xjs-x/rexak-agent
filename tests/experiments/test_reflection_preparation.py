"""第五阶段开发回归，非独立准确率或真实模型收益。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from experiments.history_action_reflection_preparation_v1.benchmark import (
    ROOT,
    original_service,
    write,
)
from experiments.history_action_reflection_preparation_v1.offline import run
from experiments.history_action_reflection_preparation_v1.reflection import (
    FeedbackLedger,
    all_tools_paired,
)
from experiments.history_action_trusted_shadow_v1 import dataset
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_workbench.tools import history_action_shadow as active


def test_same_fixed_frames_preserve_exact_detector_and_sources(monkeypatch):
    frames = json.loads((ROOT.parent / "history_action_trusted_shadow_v1/frozen/inputs.json").read_text(encoding="utf-8"))
    baseline = original_service()._compare.__globals__["adapt_history"]
    optimized = dataset.adapt_history
    rows = []
    for frame in frames:
        monkeypatch.setattr(dataset, "adapt_history", baseline)
        old = dataset.predict(frame)
        monkeypatch.setattr(dataset, "adapt_history", optimized)
        new = dataset.predict(frame)
        assert old["observation"] == new["observation"]
        assert old["adapter_complete"] == new["adapter_complete"]
        rows.append({"case": frame["case"], "exact_observation_equal": True,
                     "diagnostic_equal": old["reasons"] == new["reasons"]})
    path = ROOT / "verification/fixed-frame-regression.json"
    if not path.exists():
        write(path, {"rows": rows, "role": "SEEN_DEVELOPMENT_REGRESSION_NOT_INDEPENDENT"})


@pytest.mark.parametrize("arm,react,expected_jobs,hints", [("A", False, 3, 0), ("B", False, 3, 0),
                                                        ("C", False, 3, 1), ("C", True, 2, 1)])
async def test_passive_next_boundary_original_call_and_agent_choice(tmp_path, arm, react, expected_jobs, hints):
    row = await run(tmp_path / arm, arm, react)
    assert row["physical_mock_jobs"] == expected_jobs
    assert row["model_requests"] == expected_jobs + 1
    assert len(row["injections"]) == hints
    if hints:
        assert row["injections"][0]["after_tool_results"] == 2
        assert row["injections"][0]["message"]["type"] == "human"
    assert len(row["tool_calls"]) == len(row["tool_messages"]) == expected_jobs
    assert [x["id"] for x in row["tool_calls"]] == [x["tool_call_id"] for x in row["tool_messages"]]


async def test_checkpoint_and_receipt_recreation_no_repeated_injection(tmp_path):
    row = await run(tmp_path, "C", False, recreate=True)
    assert row["physical_mock_jobs"] == 3 and len(row["injections"]) == 1


def test_ledger_scope_material_version_and_at_most_once(tmp_path):
    payload = {"material_key": "task-version:1-evidence:1"}
    ledger = FeedbackLedger(tmp_path, "run1", "thread1")
    assert ledger.enqueue(payload)
    assert ledger.claim_next() is not None
    assert FeedbackLedger(tmp_path, "run1", "thread1").claim_next() is None
    assert not ledger.enqueue(payload)
    assert ledger.enqueue({"material_key": "task-version:2-evidence:1"})
    assert ledger.claim_next() is not None
    assert FeedbackLedger(tmp_path, "run2", "thread1").claim_next() is None


def test_unpaired_and_duplicate_tool_id_do_not_accept_notice_boundary():
    ai = AIMessage(content="", tool_calls=[{"id": "a", "name": "x", "args": {}}])
    tool = ToolMessage(content="ok", tool_call_id="a")
    assert not all_tools_paired([ai])
    assert all_tools_paired([HumanMessage(content="task"), ai, tool])
    assert not all_tools_paired([ai, ai, tool])
    assert not all_tools_paired([tool])


def test_fast_no_authorization_does_not_copy_arbitrary_context(tmp_path, monkeypatch):
    from experiments.history_action_autonomous_shadow_v1.fixture import make_box

    from agent_workbench.tools.history_action_shadow import ShadowService

    box = make_box(tmp_path)
    service = ShadowService(box)
    class MustNotCopy:
        def __deepcopy__(self, memo):
            raise AssertionError("untrusted_context_should_not_be_copied")
    async def check():
        try:
            service.enqueue({}, {"operation_id": "navigation"}, "nav",
                            {"thread_id": "t", "messages": MustNotCopy()})
            await service.drain()
            assert service.outcomes[0]["classification"] == "NOT_IN_SCOPE"
            assert not service.outcomes[0]["emit_shadow_event"]
        finally:
            await service.aclose()
            await box.executor.close()
    import asyncio
    asyncio.run(check())


def test_disabled_hook_does_not_allocate_service(tmp_path):
    from experiments.history_action_autonomous_shadow_v1.fixture import make_box
    box = make_box(tmp_path, False)
    active.enqueue_http(box, {}, {}, "x", None)
    assert box.history_action_shadow is None


def test_neutral_notice_never_copies_artifact_text():
    path = ROOT / "offline-results-v2.json"
    row = next(r for r in json.loads(path.read_text(encoding="utf-8"))["rows"] if r["arm"] == "C")
    message = row["injections"][0]["message"]["content"]
    assert "controlled response; status is not a vulnerability conclusion" not in message
    assert "可以继续原计划" in message
    assert "current_tool_call" in message and "verified_sources" in message


def test_corrupt_feedback_failure_keeps_normal_model_request(tmp_path):
    ledger = FeedbackLedger(tmp_path, "r", "t")
    assert ledger.enqueue({"material_key": "x"})
    next(ledger.root.glob("pending-*.json")).write_text("not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        ledger.claim_next()  # middleware explicitly catches this and forwards original request.


def test_budgets_include_final_maintenance_failed_unknown_and_tokens():
    from experiments.history_action_reflection_preparation_v1.metrics import (
        RequestBudget,
        usage_summary,
    )
    budget = RequestBudget()
    for _ in range(6):
        budget.reserve("ordinary", 100, 100)
        budget.complete({"total_tokens": 150}, "completed")
    for _ in range(2):
        budget.reserve("maintenance", 100, 100)
        budget.complete({"total_tokens": 150}, "completed")
    with pytest.raises(RuntimeError, match="physical_request_budget"):
        budget.reserve("ordinary", 100, 100)
    assert len(budget.ledger) == 8
    pending = RequestBudget()
    pending.reserve("ordinary", 100, 100)
    with pytest.raises(RuntimeError, match="unknown_response"):
        pending.reserve("ordinary", 100, 100)
    pending.complete(None, "connection_error_unknown")
    assert pending.ordinary == 1 and pending.stopped
    assert usage_summary(pending.ledger)["all"]["total_tokens"] == "NOT_AVAILABLE"
    with pytest.raises(RuntimeError, match="token_budget"):
        RequestBudget().reserve("ordinary", 131073, 100)


def test_unreviewed_labels_cannot_produce_independent_accuracy():
    from experiments.history_action_reflection_preparation_v1.review import score
    with pytest.raises(ValueError, match="independent_labels_not_available"):
        score([{"review_status": "UNREVIEWED", "reviewers": []}], [])


def test_net_cost_does_not_turn_tool_reduction_into_success():
    from experiments.history_action_reflection_preparation_v1.metrics import net_cost, quality
    row = quality({"necessary_action_ids": ["new-identity"], "performed_action_ids": [],
                   "independently_labeled_worthless_repeats": []})
    assert row["necessary_test_retention"] == 0 and row["missing_necessary_actions"]
    cost = net_cost({"elapsed_ms": 100, "provider_requests": 2, "tool_calls": 2},
                    {"elapsed_ms": 90, "provider_requests": 1, "tool_calls": 1})
    assert cost["currency_saved"] == "NOT_AVAILABLE" and not cost["first_triggering_call_saved"]


async def test_feedback_failure_returns_original_tool_and_model_results(tmp_path):
    from types import SimpleNamespace

    from experiments.history_action_reflection_preparation_v1.offline import PassiveMiddleware

    class BrokenBox:
        @property
        def history_action_shadow(self):
            raise OSError("simulated observer failure")

    ledger = FeedbackLedger(tmp_path, "r", "t")
    ledger.enqueue({"material_key": "x"})
    next(ledger.root.glob("pending-*.json")).write_text("broken", encoding="utf-8")
    middleware = PassiveMiddleware(BrokenBox(), "C", ledger)
    request = SimpleNamespace(messages=[])
    result = ToolMessage(content="unaltered", tool_call_id="executed")
    async def handler(value):
        assert value is request
        return result
    assert await cast(Any, middleware).awrap_tool_call(request, handler) is result
    assert await cast(Any, middleware).awrap_model_call(request, handler) is result
    assert middleware.errors == ["OSError", "JSONDecodeError"]


def test_receipt_claim_is_visible_after_new_python_process(tmp_path):
    import subprocess
    import sys

    ledger = FeedbackLedger(tmp_path, "r", "t")
    assert ledger.enqueue({"material_key": "x"})
    script = (
        "from pathlib import Path; import sys; "
        "from experiments.history_action_reflection_preparation_v1.reflection import FeedbackLedger; "
        "assert FeedbackLedger(Path(sys.argv[1]), 'r', 't').claim_next() is not None"
    )
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=20)
    assert ledger.claim_next() is None
