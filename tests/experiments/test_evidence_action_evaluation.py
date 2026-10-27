"""评价层与检测层隔离、确定性和真实决策前时点验证。"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from experiments.evidence_action_control_v1.evaluate import (
    ROOT,
    audit_oracle_sources,
    encoded,
    output_directory,
    preserve_existing,
    run_observations,
    score_observations,
)
from experiments.evidence_action_control_v1.replay import (
    build_real_snapshots,
    build_recorded_offline_snapshots,
)
from experiments.evidence_action_control_v1.synthetic import build_synthetic_snapshots


def test_complete_evaluation_keeps_every_independent_oracle_sample():
    snapshots = [*build_real_snapshots(), *build_synthetic_snapshots()]
    before = encoded([s.model_dump(mode="json") for s in snapshots])
    observations = run_observations(snapshots)
    oracle = json.loads((ROOT / "evaluation_oracle.json").read_text(encoding="utf-8"))
    scored = score_observations(observations, oracle)
    assert scored["sample_count"] == len(oracle["samples"]) == len(snapshots)
    assert len(scored["per_sample"]) == len(snapshots)
    assert all(o.decision_mode == "OBSERVE_ONLY" for o in observations)
    assert before == encoded([s.model_dump(mode="json") for s in snapshots])
    # 这是审计完整性测试，不把全部Oracle标签命中写成必过断言或隐藏反例。
    assert scored["real_provider_requests"] == scored["interception_count"] == 0


def test_output_cannot_overwrite_old_material_or_initial_iteration():
    assert output_directory(ROOT / "results") == ROOT / "results"
    for path in (ROOT.parent / "decision_memory_live_v1", ROOT / "iterations/initial", ROOT):
        with pytest.raises(ValueError, match="output_must_stay"):
            output_directory(path)


def test_oracle_source_hash_disagreement_is_recorded_without_changing_labels():
    oracle = {"source_sha256": {"experiments/evidence_action_control_v1/contracts.py": "0" * 64}}
    before = copy.deepcopy(oracle)
    audit = audit_oracle_sources(oracle)
    assert audit["status"] == "PROVENANCE_GAP"
    assert audit["sources"][0]["matches"] is False
    assert oracle == before


def test_recorded_boundary_observations_preserve_unknown_and_blocked():
    snapshots = build_recorded_offline_snapshots()
    before = encoded([s.model_dump(mode="json") for s in snapshots])
    for snapshot, observation in zip(snapshots, run_observations(snapshots), strict=True):
        assert observation.evidence_sufficiency.status != "SUFFICIENT"
        assert observation.evidence_sufficiency.supported_conclusions == []
        if snapshot.candidate.name == "read_local_execution":
            assert observation.repeat_risk.unknown_execution_risk
            assert observation.repeat_risk.status == "LOW"
        if snapshot.candidate.name == "simulate_check":
            assert observation.equivalence.classification == "CONDITION_CHANGED"
            assert "prerequisites.K" in observation.information_gain.condition_differences
            assert "artifact:84a1edac-4de5-4f27-9732-5dd5fbced02d" in observation.information_gain.supporting_evidence_refs
    assert before == encoded([s.model_dump(mode="json") for s in snapshots])


def test_prerequisite_from_different_environment_does_not_resolve_original_blocker():
    snapshot = next(s for s in build_recorded_offline_snapshots()
                    if s.candidate.name == "simulate_check")
    evidence = []
    for item in snapshot.evidence:
        if item.record_ref == "artifact:84a1edac-4de5-4f27-9732-5dd5fbced02d":
            payload = json.loads(item.content or "{}")
            payload["environment"] = "R"
            content = json.dumps(payload)
            item = item.model_copy(update={
                "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "conditions": {**item.conditions, "environment": "R"},
            })
        evidence.append(item)
    observation = run_observations([snapshot.model_copy(update={"evidence": evidence})])[0]
    assert "prerequisites.K" not in observation.information_gain.condition_differences


def test_oracle_changes_only_evaluation_never_detector_output():
    snapshot = build_synthetic_snapshots()[0]
    observed = run_observations([snapshot])
    oracle = {
        "samples": [
            {
                "sample_id": snapshot.snapshot_id,
                "origin": "synthetic",
                "rationale": "独立评分负例",
                "expected": {
                    "equivalence": ["UNRELATED_ACTION"],
                    "logical_classification": ["UNRELATED_ACTION"],
                    "repeat_risk": ["LOW"],
                    "evidence_sufficiency": ["INSUFFICIENT"],
                    "estimated_action_value": ["HIGH"],
                },
            }
        ]
    }
    scores = score_observations(observed, oracle)
    assert scores["mismatches"]
    assert observed == run_observations([snapshot])
    with pytest.raises(ValueError, match="sample_identity"):
        score_observations([*observed, *observed], oracle)


def test_same_candidate_before_and_after_body_read_changes_sufficiency_without_future_data():
    snapshots = build_real_snapshots()
    initial_read = next(
        s
        for s in snapshots
        if s.snapshot_id.startswith("real-C-p1-")
        and s.candidate.kind == "read"
        and s.candidate.artifact_ref == "artifact:e00066fc-37ab-47bd-b7fc-69fa90216f66"
    )
    later_test = next(
        s
        for s in snapshots
        if s.snapshot_id.startswith("real-C-p2-")
        and s.candidate.arguments.get("configuration") == "v1"
    )
    before = initial_read.model_copy(update={"candidate": later_test.candidate})
    left, right = run_observations([before, later_test])
    assert left.evidence_sufficiency.status != "SUFFICIENT"
    assert right.evidence_sufficiency.status == "SUFFICIENT"
    assert left.candidate_action == right.candidate_action
    assert not any(
        e.visible and e.content and e.record_ref == "artifact:e00066fc-37ab-47bd-b7fc-69fa90216f66"
        for e in before.evidence
    )


def test_synthetic_input_order_and_mutable_aliases_do_not_change_results():
    snapshots = build_synthetic_snapshots()
    expected = {o.snapshot_id: o.model_dump() for o in run_observations(snapshots)}
    actual = {
        o.snapshot_id: o.model_dump()
        for o in run_observations(list(reversed(copy.deepcopy(snapshots))))
    }
    assert actual == expected


def test_detector_has_no_oracle_or_execution_dependency():
    import ast

    for name in ("evaluator.py", "contracts.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(
            any(
                word in module
                for word in (
                    "oracle",
                    "replay",
                    "requests",
                    "httpx",
                    "openai",
                    "executor",
                    "subprocess",
                )
            )
            for module in imported
        )
        assert not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"open", "eval", "exec"}
            for n in ast.walk(tree)
        )
    # 不依赖报告、原评分、未来模型最终答复作输入。
    assert "evaluation_oracle" not in (ROOT / "evaluator.py").read_text(encoding="utf-8")


def test_preexisting_sources_freezes_and_live_ledgers_preserved():
    assert preserve_existing()["changed_files"] == []


def test_artifact_instruction_injection_does_not_change_rule_output():
    snapshots = {s.snapshot_id: s for s in build_synthetic_snapshots()}
    clean = run_observations([snapshots["synthetic-exact-repeat"]])[0]
    hostile = run_observations([snapshots["synthetic-untrusted-artifact-instructions"]])[0]
    assert clean.equivalence.classification == hostile.equivalence.classification
    assert clean.evidence_sufficiency.status == hostile.evidence_sufficiency.status
    assert clean.repeat_risk.status == hostile.repeat_risk.status
