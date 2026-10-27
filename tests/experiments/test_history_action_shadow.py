"""离线输入来源、隔离、去重序列与评分测试，不运行已封存评价入口。"""

from __future__ import annotations

import ast
import copy
import json

from experiments.history_aware_action_reflection_v1.contracts import Snapshot
from experiments.history_aware_action_reflection_v1.detector import observe_input
from experiments.history_aware_action_reflection_v1.evaluate import (
    ROOT,
    preservation,
    score,
    shadow_sequence,
)
from experiments.history_aware_action_reflection_v1.fixtures import base
from experiments.history_aware_action_reflection_v1.replay import adapt, load_seen


def test_old_all_seen_sets_are_replayed_without_calling_old_executors():
    values = load_seen()
    assert {k: len(v) for k, v in values.items()} == {
        "seen-development": 42,
        "seen-boundary": 8,
        "seen-former-holdout": 24,
    }
    for snapshots in values.values():
        for s in snapshots:
            before = s.model_dump()
            result = observe_input(s)
            assert not result.emit_shadow_event
            assert before == s.model_dump()
    real = [s for s in values["seen-development"] if s.snapshot_id.startswith("real-")]
    assert len(real) == 16
    tests = [s for s in real if s.candidate.tool_name == "simulate_check"]
    assert len(tests) == 4
    for s in tests:
        result = observe_input(s)
        assert result.classification == "UNKNOWN_SEMANTICS"
        assert result.related_history
        assert any("executor_id" in m.differences for m in result.related_history)


def test_c_same_batch_candidates_share_predecision_evidence_not_future_results():
    from experiments.evidence_action_control_v1.contracts import ActionSnapshot

    path = ROOT.parent / "evidence_action_control_reliability_v1/inputs/development-snapshots.json"
    old = [
        ActionSnapshot.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8"))
    ]
    c = [s for s in old if s.snapshot_id.startswith("real-C-p2")]
    assert len(c) == 2 and c[0].evidence == c[1].evidence
    for s in c:
        value = adapt(s, "seen-fixture")
        assert {e.evidence_ref for e in value.evidence if e.visible} == {
            e.record_ref for e in s.evidence if e.visible
        }
        assert not any(
            e.source_tool_call_id in value.same_batch_tool_call_ids
            for e in value.evidence
            if e.source_tool_call_id
        )


def test_repeated_shadow_sequence_emits_only_once_and_does_not_mutate_snapshots():
    s = Snapshot.model_validate(base())
    before = copy.deepcopy(s.model_dump())
    results = shadow_sequence([s] * 12)
    assert sum(r.emit_shadow_event for r in results) == 1
    assert sum(r.duplicate_suppressed for r in results) == 11
    assert all(r.dedup_key == results[0].dedup_key for r in results)
    assert s.model_dump() == before


def test_label_changes_affect_scoring_not_predictions():
    s = Snapshot.model_validate(base())
    observed = observe_input(s)
    row = {
        "snapshot_id": s.snapshot_id,
        "reference_should_signal": True,
        "strict_category": "TRIGGER_CANDIDATE",
        "retrieval_expected": ["operation:old"],
        "stratum": "unit",
        "rationale": "unit",
    }
    assert score([observed], [row])["confusion"]["tp"] == 1
    changed = {**row, "reference_should_signal": False}
    assert score([observed], [changed])["confusion"]["fp"] == 1
    assert observed == observe_input(s)


def test_core_does_not_import_oracle_fixtures_io_or_execution_clients():
    text = (ROOT / "detector.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    forbidden = ("oracle", "fixtures", "evaluate", "httpx", "openai", "subprocess", "replay")
    assert not any(any(word in m for word in forbidden) for m in modules)
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"open", "eval", "exec"}
        for n in ast.walk(tree)
    )


def test_prior_experiments_and_production_sources_are_byte_preserved():
    assert preservation()["status"] == "PRESERVED"
