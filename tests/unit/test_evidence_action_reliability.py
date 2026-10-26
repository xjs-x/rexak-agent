"""第二轮通用语义验证；不加载 Oracle、留出集或执行候选。"""

from __future__ import annotations

from experiments.evidence_action_control_reliability_v1.evaluator import evaluate, evaluate_input

from tests.unit.test_evidence_action_evaluator import _body, _candidate, _conditions, _snapshot


def test_explicit_candidate_argument_conflict_does_not_prove_distinct_or_safe():
    s = _candidate(_snapshot(), arguments={"url": "https://example.invalid/other"})
    o = evaluate(s)
    assert o.equivalence.classification == "INSUFFICIENT_INFORMATION"
    assert o.repeat_risk.status == "UNCERTAIN"
    assert o.information_gain.classification == "UNCERTAIN"
    assert o.candidate_action.arguments == s.candidate.arguments


def test_changed_graph_revision_adds_possible_navigation_information():
    s = _candidate(
        _snapshot(), name="get_graph", kind="query", arguments={}, conditions={}, operation_ref=None
    )
    s = s.model_copy(
        update={
            "evidence": [],
            "boundary": {
                "graph_context": {
                    "revision": 7,
                    "visible_revision": 6,
                    "provenance_verified": True,
                    "source_refs": ["tool:update"],
                    "visible_arguments": {},
                }
            },
        }
    )
    o = evaluate(s)
    assert o.equivalence.classification == "CONDITION_CHANGED"
    assert o.information_gain.classification == "POTENTIAL"
    assert o.repeat_risk.status == "LOW"


def test_same_graph_revision_default_scope_is_redundant_read_not_execution():
    s = _candidate(
        _snapshot(),
        name="get_graph",
        kind="query",
        arguments={"include_pending": True},
        conditions={},
        operation_ref=None,
    )
    s = s.model_copy(
        update={
            "boundary": {
                "graph_context": {
                    "revision": 7,
                    "visible_revision": 7,
                    "provenance_verified": True,
                    "source_refs": ["tool:get"],
                    "visible_arguments": {},
                }
            }
        }
    )
    o = evaluate(s)
    assert o.equivalence.logical_classification == "SEMANTIC_REPEAT"
    assert o.information_gain.classification == "LOW" and o.repeat_risk.status == "LOW"
    different = _candidate(s, arguments={"include_pending": False})
    assert evaluate(different).equivalence.classification == "INSUFFICIENT_INFORMATION"


def test_graph_write_role_and_evidence_generation_are_distinct():
    s = _candidate(
        _snapshot(), name="update_graph", kind="other", arguments={"operations": []}, conditions={}
    )
    ordinary = evaluate(s)
    maintenance = evaluate(s.model_copy(update={"boundary": {"phase": "graph_review"}}))
    assert (
        ordinary.information_gain.cost_and_side_effects["action_family"]
        == "ordinary_graph_claim_write"
    )
    assert (
        maintenance.information_gain.cost_and_side_effects["action_family"] == "graph_maintenance"
    )
    assert (
        maintenance.information_gain.cost_and_side_effects["creates_independent_execution_evidence"]
        is False
    )
    assert maintenance.repeat_risk.status == "LOW"


def test_complete_read_covers_offset_subrange_but_partial_page_does_not():
    s = _candidate(
        _snapshot(),
        name="read_artifact",
        kind="read",
        arguments={"artifact_id": "A", "offset": 1},
        artifact_ref="artifact:A",
    )
    o = evaluate(s)
    assert o.equivalence.logical_classification == "SEMANTIC_REPEAT"
    assert o.information_gain.classification == "LOW" and o.repeat_risk.status == "LOW"
    partial = s.evidence[0].model_copy(update={"complete": False})
    o = evaluate(s.model_copy(update={"evidence": [partial]}))
    assert o.information_gain.classification == "POTENTIAL"


def test_large_archive_navigation_is_possible_relevance_not_cost_based_rejection():
    s = _candidate(
        _snapshot(),
        name="read_artifact",
        kind="read",
        arguments={"artifact_id": "archive"},
        artifact_ref="artifact:archive",
        operation_ref=None,
        estimated_cost={"source_bytes": 100000000},
    )
    e = _body().model_copy(
        update={
            "record_ref": "artifact:archive",
            "operation_ref": None,
            "visible": False,
            "complete": False,
            "provenance_verified": False,
            "content": None,
            "conditions": {
                "navigation_role": "conversation_archive",
                "relevance_refs": s.task.source_refs,
            },
        }
    )
    o = evaluate(s.model_copy(update={"evidence": [e]}))
    assert o.information_gain.classification == "POTENTIAL"
    assert o.information_gain.cost_and_side_effects["source_bytes"] == 100000000


def test_job_identifier_is_not_rewritten_into_operation():
    s = _candidate(
        _snapshot(),
        name="read_local_execution",
        kind="query",
        arguments={"operation_id": "A"},
        operation_ref="operation:A",
    )
    h = s.historical_actions[0].model_copy(update={"record_ref": "operation:other"})
    s = s.model_copy(update={"historical_actions": [h]})
    o = evaluate(s)
    assert "identity_namespace_mismatch" in o.equivalence.unknown_dimensions
    assert o.candidate_action.arguments == {"operation_id": "A"}
    assert o.information_gain.classification == "UNCERTAIN" and o.repeat_risk.status == "LOW"


def test_changed_config_is_not_repeat_or_necessary_for_history_explanation():
    s = _candidate(_snapshot(), conditions=_conditions(configuration="v2"))
    o = evaluate(s)
    assert o.equivalence.logical_classification == "CONDITION_CHANGED"
    assert o.information_gain.classification == "LOW" and o.repeat_risk.status == "LOW"


def test_genuinely_current_different_identity_remains_possible_not_high_risk():
    s = _snapshot()
    task = s.task.model_copy(update={"goal": "verify_current"})
    s = _candidate(
        s.model_copy(update={"task": task}), conditions=_conditions(identity_id="identity:new")
    )
    o = evaluate(s)
    assert o.information_gain.classification == "POTENTIAL"
    assert o.repeat_risk.status != "HIGH"


def test_sufficient_current_task_does_not_promote_unrelated_history_read():
    s = _snapshot()
    extra = _body(check="elsewhere", environment="other").model_copy(
        update={
            "record_ref": "artifact:elsewhere",
            "operation_ref": "operation:elsewhere",
            "visible": False,
            "content": None,
            "conditions": {"route": "elsewhere", "environment": "other"},
        }
    )
    s = _candidate(
        s.model_copy(update={"evidence": [*s.evidence, extra]}),
        name="read_artifact",
        kind="read",
        arguments={"artifact_id": "elsewhere"},
        artifact_ref="artifact:elsewhere",
        operation_ref=None,
    )
    o = evaluate(s)
    assert o.evidence_sufficiency.status == "SUFFICIENT"
    assert o.information_gain.classification == "LOW"


def test_conflicting_ownership_does_not_get_high_read_recommendation():
    s = _snapshot()
    e = s.evidence[0].model_copy(
        update={"operation_ref": "operation:wrong", "visible": False, "content": None}
    )
    s = _candidate(
        s.model_copy(update={"evidence": [e]}),
        name="read_artifact",
        kind="read",
        artifact_ref="artifact:A",
        arguments={"artifact_id": "A"},
    )
    assert evaluate(s).information_gain.classification == "UNCERTAIN"


def test_invalid_input_and_unknown_semantics_are_observation_only():
    assert evaluate_input({"bad": object()}).evidence_sufficiency.status == "INDETERMINATE"
    s = _candidate(_snapshot(), name="unsupported", kind="other", conditions={})
    assert evaluate(s).information_gain.classification == "UNCERTAIN"
    assert evaluate(s).decision_mode == "OBSERVE_ONLY"
