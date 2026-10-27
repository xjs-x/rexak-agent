"""封存来源后一次性离线评价；不加载 Provider，不执行候选工具。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from experiments.evidence_action_control_reliability_v1.evaluator import evaluate
from experiments.evidence_action_control_reliability_v1.prepare_inputs import (
    build_inputs,
    sha,
    write_once,
)
from experiments.evidence_action_control_v1.contracts import ActionSnapshot
from experiments.evidence_action_control_v1.evaluate import FIELDS, score_observations

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def preservation():
    original = load(ROOT / "inputs/preservation-before.json")
    changed = [name for name, digest in original.items() if sha(REPO / name) != digest]
    if changed:
        raise RuntimeError(f"preexisting_material_changed:{changed}")
    return {"checked_files": len(original), "changed_files": [], "status": "PRESERVED"}


def verify_inputs():
    frozen = load(ROOT / "inputs/freeze.json")
    source_note = None
    for name, digest in frozen["files"].items():
        path = REPO / name
        if sha(path) != digest:
            # 保留最早封存的源码缺口；当前精确源码重新验证输入并独立封存。
            if name.endswith("/prepare_inputs.py"):
                development, boundary = build_inputs()
                if development != load(
                    ROOT / "inputs/development-snapshots.json"
                ) or boundary != load(ROOT / "inputs/boundary-snapshots.json"):
                    raise RuntimeError("current_adapter_does_not_reproduce_fixed_inputs")
                source_note = {
                    "earliest_frozen_adapter_sha256": digest,
                    "earliest_adapter_bytes": "NOT_RECOVERED_NO_CLAIM_OF_ORIGINAL_SOURCE_IDENTITY",
                    "current_adapter_sha256": sha(path),
                    "current_source_reproduces_fixed_input_objects": True,
                    "current_source_bound_by_new_final_freeze": True,
                    "inputs_and_labels_unchanged": True,
                }
            else:
                raise RuntimeError(f"input_freeze_mismatch:{name}")
    sealed = load(ROOT / "heldout/seal.json")
    for name, digest in sealed["files"].items():
        if sha(ROOT / "heldout" / name) != digest:
            raise RuntimeError(f"heldout_seal_mismatch:{name}")
    return {"inputs": "MATCHED", "heldout": "MATCHED", "source_format_lineage": source_note}


def freeze():
    if (ROOT / "ATTEMPT.json").exists() or (ROOT / "freeze.json").exists():
        raise RuntimeError("already_frozen_or_attempted")
    verification = verify_inputs()
    paths = [
        *ROOT.glob("*.py"),
        *ROOT.glob("inputs/*.json"),
        *ROOT.glob("heldout/*"),
        ROOT / "diagnostics/recovered-synthetic-6401641b.py",
        *REPO.glob("experiments/evidence_action_control_v1/*.py"),
        REPO / "tests/unit/test_evidence_action_reliability.py",
        REPO / "uv.lock",
        REPO / "pyproject.toml",
    ]
    write_once(
        ROOT / "freeze.json",
        {
            "schema": "offline_reliability_freeze/1",
            "frozen_utc": datetime.now(UTC).isoformat(),
            "phase": "BEFORE_NEW_DETECTOR_CORPUS_PREDICTIONS",
            "development": "42 previously seen samples; 16 real metadata enriched, 26 unchanged",
            "holdout": "24 author-isolated non-human-blind samples, one evaluation only; no tuning after results",
            "rules": "task-conditioned value independent of changed conditions; conservative unresolved semantics",
            "input_verification": verification,
            "preservation": preservation(),
            "python": sys.version,
            "packages": {n: importlib.metadata.version(n) for n in ("pydantic", "pytest")},
            "files": {
                p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(paths)) if p.is_file()
            },
            "real_provider_requests": 0,
            "production_execution_changes": 0,
        },
    )
    print(json.dumps({"frozen_sha256": sha(ROOT / "freeze.json")}))


def verify():
    frozen = load(ROOT / "freeze.json")
    for name, digest in frozen["files"].items():
        if sha(REPO / name) != digest:
            raise RuntimeError(f"experiment_freeze_mismatch:{name}")
    return {
        "freeze_sha256": sha(ROOT / "freeze.json"),
        **verify_inputs(),
        "preservation": preservation(),
    }


def observe(name, path):
    rows = load(path)
    snapshots = [ActionSnapshot.model_validate(row) for row in rows]
    observations = [evaluate(snapshot) for snapshot in snapshots]
    write_once(
        ROOT / f"results/{name}-observations.json",
        [o.model_dump(mode="json") for o in observations],
    )
    return snapshots, observations


def score(name, observations, oracle_path):
    # 预测已经持久化后才打开对应标签，不将标签传给观察函数。
    oracle = load(oracle_path)
    metrics = score_observations(observations, oracle)
    per_origin = {}
    for origin in sorted({row["origin"] for row in oracle["samples"]}):
        subset = {row["sample_id"] for row in oracle["samples"] if row["origin"] == origin}
        group = score_observations(
            [o for o in observations if o.snapshot_id in subset],
            {"samples": [row for row in oracle["samples"] if row["sample_id"] in subset]},
        )
        per_origin[origin] = {
            k: group[k] for k in ("sample_count", "all_fields_matched", "field_results")
        }
    useful = []
    high_for_useful = []
    expected_by_id = {r["sample_id"]: r for r in oracle["samples"]}
    for observation in observations:
        row = expected_by_id[observation.snapshot_id]
        allowed = row["expected"]["estimated_action_value"]
        allowed = [allowed] if isinstance(allowed, str) else allowed
        if set(allowed) <= {"HIGH", "POTENTIAL"}:
            useful.append(observation.snapshot_id)
            if observation.repeat_risk.status == "HIGH":
                high_for_useful.append(observation.snapshot_id)
    metrics.update(
        per_origin=per_origin,
        useful_actions={
            "definition": "Oracle requires only HIGH/POTENTIAL value, without LOW/UNCERTAIN alternatives",
            "count": len(useful),
            "incorrect_high_repeat_risk": high_for_useful,
        },
        uncertainty_outputs={
            f: sum(
                getattr(o, outer).__getattribute__(inner)
                in {"UNCERTAIN", "INSUFFICIENT_INFORMATION", "INDETERMINATE"}
                for o in observations
            )
            for f, (outer, inner) in FIELDS.items()
        },
    )
    write_once(ROOT / f"results/{name}-metrics.json", metrics)
    return metrics


def run():
    verification = verify()
    write_once(
        ROOT / "ATTEMPT.json",
        {
            "started_utc": datetime.now(UTC).isoformat(),
            **verification,
            "once_only": True,
            "real_provider_requests": 0,
        },
    )
    try:
        _, raw = observe("original-inputs", ROOT / "inputs/original-development-snapshots.json")
        original = score("original-inputs", raw, ROOT / "inputs/evaluation_oracle.json")
        _, enriched = observe("development", ROOT / "inputs/development-snapshots.json")
        development = score("development", enriched, ROOT / "inputs/evaluation_oracle.json")
        snapshots, boundary = observe("boundary", ROOT / "inputs/boundary-snapshots.json")
        checks = []
        for snapshot, observation in zip(snapshots, boundary, strict=True):
            checks.append(
                {
                    "sample_id": snapshot.snapshot_id,
                    "not_sufficient": observation.evidence_sufficiency.status != "SUFFICIENT",
                    "no_effect_conclusion": not observation.evidence_sufficiency.supported_conclusions,
                    "original_unknown_preserved": (
                        observation.repeat_risk.unknown_execution_risk
                        and observation.repeat_risk.status == "LOW"
                    )
                    if snapshot.candidate.name == "read_local_execution"
                    else None,
                    "prerequisite_change_not_effect": (
                        observation.equivalence.classification == "CONDITION_CHANGED"
                        and "prerequisites.K" in observation.information_gain.condition_differences
                    )
                    if snapshot.candidate.name == "simulate_check"
                    else None,
                }
            )
        write_once(
            ROOT / "results/boundary-checks.json",
            {
                "sample_count": len(checks),
                "checks": checks,
                "all_checks_passed": all(
                    v is not False for r in checks for k, v in r.items() if k != "sample_id"
                ),
                "dimensions": {
                    f: dict(Counter(getattr(o, outer).__getattribute__(inner) for o in boundary))
                    for f, (outer, inner) in FIELDS.items()
                },
                "oracle": "No new 0-2 semantic score; original recorded boundary invariants retained",
            },
        )
        # 不在此之前读取留出输入内容，也不在得到结果后改变规则重评。
        _, heldout = observe("heldout", ROOT / "heldout/inputs.json")
        heldout_metrics = score("heldout", heldout, ROOT / "heldout/oracle.json")
        write_once(
            ROOT / "results/completion.json",
            {
                "status": "COMPLETED_ONCE",
                "completed_utc": datetime.now(UTC).isoformat(),
                "freeze_sha256": sha(ROOT / "freeze.json"),
                "preservation": preservation(),
                "all_fields_matched": {
                    "original_inputs": original["all_fields_matched"],
                    "development": development["all_fields_matched"],
                    "heldout": heldout_metrics["all_fields_matched"],
                },
                "real_provider_requests": 0,
                "candidate_executions": 0,
                "interceptions": 0,
            },
        )
        print(json.dumps(load(ROOT / "results/completion.json"), ensure_ascii=False))
    except Exception as exc:
        write_once(
            ROOT / "STOP.json",
            {"type": type(exc).__name__, "error": str(exc), "retry_forbidden": True},
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "verify", "run"))
    command = parser.parse_args().command
    if command == "freeze":
        freeze()
    elif command == "verify":
        print(json.dumps(verify(), ensure_ascii=False))
    else:
        run()
