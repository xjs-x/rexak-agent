"""先生成无 Oracle 的观察，再独立评分；默认仅读既存本地轨迹。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from experiments.evidence_action_control_v1.contracts import ActionObservation, ActionSnapshot
from experiments.evidence_action_control_v1.evaluator import evaluate as observe
from experiments.evidence_action_control_v1.replay import (
    build_real_snapshots,
    build_recorded_offline_snapshots,
)
from experiments.evidence_action_control_v1.synthetic import build_synthetic_snapshots

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
FIELDS = {
    "equivalence": ("equivalence", "classification"),
    "logical_classification": ("equivalence", "logical_classification"),
    "evidence_sufficiency": ("evidence_sufficiency", "status"),
    "estimated_action_value": ("information_gain", "estimated_action_value"),
    "repeat_risk": ("repeat_risk", "status"),
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def run_observations(snapshots: list[ActionSnapshot]) -> list[ActionObservation]:
    """此入口没有 Oracle 参数；结果由输入快照唯一决定。"""
    before = encoded([s.model_dump(mode="json") for s in snapshots])
    observations = [observe(s) for s in snapshots]
    if before != encoded([s.model_dump(mode="json") for s in snapshots]):
        raise RuntimeError("observation_mutated_input")
    return observations


def score_observations(
    observations: list[ActionObservation], oracle: dict[str, Any]
) -> dict[str, Any]:
    """评价阶段才读独立预期；允许标签集合如实单列，失败不自动改标签。"""
    expected = {row["sample_id"]: row for row in oracle["samples"]}
    if (
        len(expected) != len(oracle["samples"])
        or len(observations) != len(expected)
        or set(expected) != {o.snapshot_id for o in observations}
    ):
        raise ValueError("evaluation_sample_identity_mismatch")
    counts: dict[str, Counter[str]] = {field: Counter() for field in FIELDS}
    confusion: dict[str, dict[str, Counter[str]]] = {
        field: defaultdict(Counter) for field in FIELDS
    }
    errors, matched, per_sample = [], 0, []
    risk_confusion = Counter(
        {
            "true_positive": 0,
            "false_negative": 0,
            "false_positive": 0,
            "true_negative": 0,
            "set_valued_excluded": 0,
        }
    )
    logical_confusion = Counter(risk_confusion)
    origins = Counter()
    for observation in observations:
        row = expected[observation.snapshot_id]
        actual = observation.model_dump(mode="json")
        outcomes = {}
        origins[row["origin"]] += 1
        for field, (outer, inner) in FIELDS.items():
            value = actual[outer][inner]
            allowed = row["expected"][field]
            if isinstance(allowed, str):
                allowed = [allowed]
            good = value in allowed
            outcomes[field] = good
            counts[field]["matched" if good else "mismatched"] += 1
            counts[field][f"predicted:{value}"] += 1
            confusion[field]["|".join(sorted(allowed))][value] += 1
            if not good:
                errors.append(
                    {
                        "sample_id": observation.snapshot_id,
                        "field": field,
                        "expected": allowed,
                        "actual": value,
                        "rationale": row["rationale"],
                    }
                )
        all_matched = all(outcomes.values())
        matched += int(all_matched)
        risk_expected = row["expected"]["repeat_risk"]
        risk_expected = [risk_expected] if isinstance(risk_expected, str) else risk_expected
        positive = observation.repeat_risk.status == "HIGH"
        if risk_expected == ["HIGH"]:
            risk_confusion["true_positive" if positive else "false_negative"] += 1
        elif "HIGH" not in risk_expected:
            risk_confusion["false_positive" if positive else "true_negative"] += 1
        else:
            risk_confusion["set_valued_excluded"] += 1
        repeat_labels = {"EXACT_REPEAT", "SEMANTIC_REPEAT"}
        logical_expected = row["expected"]["logical_classification"]
        logical_expected = (
            {logical_expected} if isinstance(logical_expected, str) else set(logical_expected)
        )
        predicted_repeat = observation.equivalence.logical_classification in repeat_labels
        if logical_expected <= repeat_labels:
            logical_confusion["true_positive" if predicted_repeat else "false_negative"] += 1
        elif not logical_expected & repeat_labels:
            logical_confusion["false_positive" if predicted_repeat else "true_negative"] += 1
        else:
            logical_confusion["set_valued_excluded"] += 1
        per_sample.append(
            {
                "sample_id": observation.snapshot_id,
                "origin": row["origin"],
                "all_fields_match": all_matched,
                "field_matches": outcomes,
            }
        )
    tp, fp, fn = (risk_confusion[k] for k in ("true_positive", "false_positive", "false_negative"))
    return {
        "scope": "CURRENT_OFFLINE_SET_ONLY_NOT_GENERALIZATION",
        "sample_count": len(observations),
        "origins": dict(origins),
        "all_fields_matched": matched,
        "field_results": {k: dict(v) for k, v in counts.items()},
        "confusion": {
            f: {e: dict(pred) for e, pred in rows.items()} for f, rows in confusion.items()
        },
        "high_repeat_risk": {
            **dict(risk_confusion),
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "definition": "Predicted HIGH vs independently required HIGH; set-valued inclusion of HIGH excluded",
        },
        "logical_repeat_detection": {
            **dict(logical_confusion),
            "precision": logical_confusion["true_positive"]
            / (logical_confusion["true_positive"] + logical_confusion["false_positive"])
            if logical_confusion["true_positive"] + logical_confusion["false_positive"]
            else None,
            "recall": logical_confusion["true_positive"]
            / (logical_confusion["true_positive"] + logical_confusion["false_negative"])
            if logical_confusion["true_positive"] + logical_confusion["false_negative"]
            else None,
        },
        "mismatches": errors,
        "per_sample": per_sample,
        "interception_count": 0,
        "real_provider_requests": 0,
    }


def preserve_existing() -> dict[str, Any]:
    baseline = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    changed = [
        name
        for name, digest in baseline.items()
        if not (REPO / name).is_file() or sha(REPO / name) != digest
    ]
    if changed:
        raise RuntimeError("existing_material_changed:" + ",".join(changed))
    return {"status": "PRESERVED", "checked_files": len(baseline), "changed_files": []}


def audit_oracle_sources(oracle: dict[str, Any]) -> dict[str, Any]:
    """评分之外保留标签所记输入哈希差异，不能默默更新 Oracle。"""
    rows = []
    for name, recorded in oracle.get("source_sha256", {}).items():
        path = (REPO / name).resolve()
        actual = sha(path) if path.is_relative_to(REPO) and path.is_file() else None
        rows.append({"path": name, "recorded_sha256": recorded, "current_sha256": actual,
                     "matches": actual == recorded})
    return {"status": "MATCHED" if rows and all(row["matches"] for row in rows)
            else "PROVENANCE_GAP", "sources": rows}


def make_manifest(oracle_path: Path) -> dict[str, Any]:
    sources = sorted(
        [
            *ROOT.glob("*.py"),
            REPO / "tests/unit/test_evidence_action_evaluator.py",
            REPO / "tests/experiments/test_evidence_action_replay.py",
            REPO / "tests/experiments/test_evidence_action_evaluation.py",
            REPO / "src/agent_workbench/domain/decision_memory.py",
            REPO / "src/agent_workbench/contracts/decision_memory.py",
            REPO / "uv.lock",
            REPO / "pyproject.toml",
        ]
    )
    inputs = [
        REPO / "experiments/decision_memory_live_v1/bundle/history.json",
        REPO / "experiments/decision_memory_ab_comparison_v1/live-results/A/result.json",
        REPO / "experiments/decision_memory_ab_comparison_v1/live-results/B/result.json",
        REPO / "experiments/decision_memory_s14c_r3/live-results/S14-C-R3.json",
        REPO / "experiments/decision_memory_live_v1/preflight/S15-C.json",
        REPO / "experiments/decision_memory_live_v1/preflight/S17-C.json",
        oracle_path,
    ]
    loaded = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).is_file() and Path(filename).suffix != ".pyc":
            loaded[str(Path(filename).resolve())] = sha(Path(filename))
    return {
        "status": "OFFLINE_OBSERVATION_ONLY",
        "python": sys.version,
        "source_files": {p.relative_to(REPO).as_posix(): sha(p) for p in sources},
        "inputs": {p.relative_to(REPO).as_posix(): sha(p) for p in inputs},
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("pydantic", "langchain-core", "sqlalchemy", "pytest")
        },
        "loaded_module_files": loaded,
        "preservation": preserve_existing(),
    }


def output_directory(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT / "results"):
        raise ValueError("output_must_stay_in_new_observation_results")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    args.output = output_directory(args.output)
    snapshots = [*build_real_snapshots(), *build_synthetic_snapshots()]
    observations = run_observations(snapshots)
    # 检测完成后才加载独立 Oracle，不将标签、报告评语或正确答案传给观察核心。
    oracle_path = ROOT / "evaluation_oracle.json"
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    metrics = score_observations(observations, oracle)
    metrics["oracle_source_integrity"] = audit_oracle_sources(oracle)
    write_json(args.output / "snapshots.json", [s.model_dump(mode="json") for s in snapshots])
    write_json(args.output / "observations.json", [o.model_dump(mode="json") for o in observations])
    write_json(args.output / "metrics.json", metrics)
    boundary_snapshots = build_recorded_offline_snapshots()
    write_json(args.output / "scripted-boundary-snapshots.json",
               [s.model_dump(mode="json") for s in boundary_snapshots])
    write_json(args.output / "scripted-boundary-observations.json",
               [o.model_dump(mode="json") for o in run_observations(boundary_snapshots)])
    write_json(args.output / "manifest.json", make_manifest(oracle_path))
    write_json(args.output / "preservation-after.json", preserve_existing())
    print(
        json.dumps(
            {
                "samples": len(snapshots),
                "all_fields_matched": metrics["all_fields_matched"],
                "mismatches": len(metrics["mismatches"]),
                "real_provider_requests": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
