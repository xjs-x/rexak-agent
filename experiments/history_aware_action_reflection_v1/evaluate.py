"""独立封存、一次性预测后评分与只读核验；无 Provider/数据库/执行端入口。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import Observation, Policy, Snapshot
from .detector import canonical, observe_input, record_trigger
from .replay import REPO, SOURCES, load_seen

ROOT = Path(__file__).resolve().parent
POLICIES: tuple[Policy, ...] = ("raw_visible_v1", "receipt_allowed_v1")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def preservation() -> dict[str, Any]:
    before = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    changed = [p for p, h in before.items() if not (REPO / p).is_file() or sha(REPO / p) != h]
    if changed:
        raise ValueError("existing_files_changed:" + ",".join(changed))
    return {"checked": len(before), "changed": [], "status": "PRESERVED"}


def verify() -> dict[str, Any]:
    frozen = json.loads((ROOT / "freeze.json").read_text(encoding="utf-8"))
    for path, expected in frozen["files"].items():
        if sha(REPO / path) != expected:
            raise ValueError("frozen_file_drift:" + path)
    for path, expected in frozen["loaded_modules"].items():
        if sha(Path(path)) != expected:
            raise ValueError("frozen_dependency_drift:" + path)
    prospective = json.loads((ROOT / "prospective/freeze.json").read_text(encoding="utf-8"))
    for path, expected in prospective["files"].items():
        if sha(ROOT / "prospective" / path) != expected:
            raise ValueError("prospective_input_or_label_drift:" + path)
    if sha(ROOT / "fixtures.py") != prospective["builder_sha256"]:
        raise ValueError("prefrozen_fixture_builder_drift")
    return {"freeze_sha256": sha(ROOT / "freeze.json"), "preservation": preservation()}


def seal() -> None:
    sources = [
        *ROOT.glob("*.py"),
        *ROOT.glob("prospective/*.json"),
        ROOT / "preservation-before.json",
        REPO / "tests/unit/test_history_action_reflection.py",
        REPO / "tests/experiments/test_history_action_shadow.py",
        REPO / "src/agent_workbench/domain/decision_memory.py",
        REPO / "src/agent_workbench/contracts/decision_memory.py",
        REPO / "src/agent_workbench/tools/http.py",
        REPO / "src/agent_workbench/persistence/tables.py",
        REPO / "src/agent_workbench/agent/middleware/tool_dispatch.py",
        REPO / "uv.lock",
        REPO / "pyproject.toml",
        *(REPO / path for path in SOURCES.values()),
    ]
    modules = {}
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and Path(path).is_file() and Path(path).suffix in {".py", ".pyd", ".dll"}:
            modules[str(Path(path).resolve())] = sha(Path(path))
    write(
        ROOT / "freeze.json",
        {
            "purpose": "SHADOW_ONLY_BEFORE_PROSPECTIVE_PREDICTIONS",
            "version": 1,
            "policies_predeclared": POLICIES,
            "files": {p.relative_to(REPO).as_posix(): sha(p) for p in sources},
            "loaded_modules": modules,
            "runtime": {
                "python": sys.version,
                "platform": platform.platform(),
                "python_executable_sha256": sha(Path(sys.executable)),
                "packages": {
                    name: importlib.metadata.version(name)
                    for name in ("pydantic", "sqlalchemy", "langchain-core", "pytest")
                },
            },
            "preservation": preservation(),
        },
    )
    print(json.dumps(verify(), ensure_ascii=False))


def score(observations: list[Observation], labels: list[dict[str, Any]]) -> dict[str, Any]:
    actual = {o.snapshot_id: o for o in observations}
    if (
        len(actual) != len(observations)
        or len(labels) != len(actual)
        or {row["snapshot_id"] for row in labels} != set(actual)
    ):
        raise ValueError("sample_identity_mismatch")
    counts = Counter({"tp": 0, "fp": 0, "fn": 0, "tn": 0})
    retrieval_hits, retrieval_total, retrieval_count = 0, 0, 0
    mismatches, per_case = [], []
    categories = Counter(o.classification for o in observations)
    for row in labels:
        observed = actual[row["snapshot_id"]]
        positive, predicted = row["reference_should_signal"], observed.emit_shadow_event
        code = "tp" if positive and predicted else "fn" if positive else "fp" if predicted else "tn"
        counts[code] += 1
        expected = set(row["retrieval_expected"])
        found = {m.operation_ref for m in observed.related_history}
        retrieval_hits += len(found & expected)
        retrieval_total += len(expected)
        retrieval_count += len(found)
        if (
            observed.policy == "raw_visible_v1"
            and observed.classification != row["strict_category"]
        ):
            mismatches.append(
                {
                    "snapshot_id": row["snapshot_id"],
                    "expected": row["strict_category"],
                    "actual": observed.classification,
                    "rationale": row["rationale"],
                }
            )
        per_case.append(
            {
                "snapshot_id": row["snapshot_id"],
                "stratum": row["stratum"],
                "outcome": code,
                "classification": observed.classification,
                "retrieval_missing": sorted(expected - found),
                "event_evidence_refs": observed.original_evidence_refs,
            }
        )
    total = len(observations)
    tp, fp, fn, tn = (counts[k] for k in ("tp", "fp", "fn", "tn"))
    return {
        "dataset_scope": "NEW_PREFROZEN_SAME_AUTHOR_SYNTHETIC_NOT_BLIND_OR_POPULATION_ESTIMATE",
        "samples": total,
        "policy": observations[0].policy if observations else None,
        "confusion": dict(counts),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "false_negative_rate": fn / (tp + fn) if tp + fn else None,
        "trigger_rate": (tp + fp) / total if total else None,
        "abstention_rate": categories["UNKNOWN_SEMANTICS"] / total if total else None,
        "coverage_rate": (total - categories["UNKNOWN_SEMANTICS"] - categories["NOT_IN_SCOPE"])
        / total
        if total
        else None,
        "category_counts": dict(categories),
        "retrieval": {
            "expected_refs": retrieval_total,
            "matched_expected_refs": retrieval_hits,
            "recall": retrieval_hits / retrieval_total if retrieval_total else None,
            "returned_candidates": retrieval_count,
            "mean_candidates": retrieval_count / total if total else None,
        },
        "strict_category_mismatches": mismatches,
        "per_case": per_case,
    }


def benchmark(sample: Snapshot) -> list[dict[str, Any]]:
    rows = []
    for size in (1, 100, 1000):
        unrelated = [
            sample.history[0].model_copy(
                update={
                    "operation_ref": f"operation:bench-{index}",
                    "logical_test_key": f"check:other-{index}",
                    "conditions": {
                        **sample.history[0].conditions,
                        "target": "https://elsewhere.invalid",
                    },
                    "arguments": {"url": f"https://elsewhere.invalid/{index}"},
                }
            )
            for index in range(size - 1)
        ]
        snapshot = sample.model_copy(update={"history": [sample.history[0], *unrelated]})
        elapsed = []
        for _ in range(30):
            start = time.perf_counter_ns()
            observe_input(snapshot)
            elapsed.append((time.perf_counter_ns() - start) / 1e6)
        rows.append(
            {
                "histories": size,
                "repetitions": len(elapsed),
                "median_ms": statistics.median(elapsed),
                "p95_ms": sorted(elapsed)[28],
                "snapshot_bytes": len(canonical(snapshot.model_dump()).encode()),
                "includes_validation_and_serialization": True,
            }
        )
    return rows


def shadow_sequence(snapshots: list[Snapshot]) -> list[Observation]:
    """静态快照序列的 Shadow 去重演示；不是 Agent 主循环或工具执行器。"""
    states = []
    observations = []
    for snapshot in snapshots:
        value = snapshot.model_copy(
            update={"previous_triggers": [*snapshot.previous_triggers, *states]}
        )
        observed = observe_input(value)
        observations.append(observed)
        state = record_trigger(observed)
        if state is not None:
            states.append(state)
    return observations


def run() -> None:
    verified = verify()
    write(ROOT / "ATTEMPT.json", {"status": "STARTED_ONCE_NO_AUTOMATIC_RETRY", **verified})
    output = ROOT / "results"
    output.mkdir(exist_ok=False)
    inputs = json.loads((ROOT / "prospective/inputs.json").read_text(encoding="utf-8"))
    snapshots = [Snapshot.model_validate(row) for row in inputs]
    predictions = {}
    timings = {}
    for policy in POLICIES:
        start = time.perf_counter_ns()
        predictions[policy] = [observe_input(s, policy) for s in snapshots]
        timings[policy] = (time.perf_counter_ns() - start) / 1e6
        write(output / f"{policy}-predictions.json", [o.model_dump() for o in predictions[policy]])
    # 先保存所有预测，随后才读取标签；两档策略均已事前封存。
    labels = json.loads((ROOT / "prospective/labels.json").read_text(encoding="utf-8"))
    for policy, values in predictions.items():
        metrics = score(values, labels)
        metrics["batch_elapsed_ms"] = timings[policy]
        write(output / f"{policy}-metrics.json", metrics)
    for name, values in load_seen().items():
        observed = [observe_input(value) for value in values]
        write(output / f"{name}-snapshots.json", [s.model_dump() for s in values])
        write(output / f"{name}-predictions.json", [o.model_dump() for o in observed])
    write(output / "benchmark.json", benchmark(snapshots[0]))
    write(
        output / "dedup-sequence.json",
        [o.model_dump() for o in shadow_sequence([snapshots[0]] * 12)],
    )
    write(
        output / "COMPLETE.json",
        {
            "status": "COMPLETED_ONCE",
            **verify(),
            "real_provider_requests": 0,
            "candidate_executions": 0,
            "model_messages_injected": 0,
        },
    )
    print(json.dumps({"status": "COMPLETED_ONCE", "samples": len(snapshots)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seal", "run", "verify"))
    command = parser.parse_args().command
    if command == "seal":
        seal()
    elif command == "run":
        run()
    else:
        print(json.dumps(verify(), ensure_ascii=False))


if __name__ == "__main__":
    main()
