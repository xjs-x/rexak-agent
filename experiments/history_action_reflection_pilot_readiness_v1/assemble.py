"""Create review/statistics/freeze materials; no model or external services."""

from __future__ import annotations

import importlib.metadata
import json
import sqlite3
from collections import Counter

from .evaluate import annotation_template, summarize
from .protocol import REPO, ROOT, sha, write
from .verify import preservation


def main():
    receipts = json.loads((ROOT / "results/request-ledger.json").read_text(encoding="utf-8"))[
        "rows"
    ]
    metrics = []
    for path in sorted((ROOT / "results/runs").glob("*/trace.json")):
        trace = json.loads(path.read_text(encoding="utf-8"))
        metrics.append(summarize(trace, receipts))
        write(path.parent / "human-quality-template.json", annotation_template(sha(path)))
    write(
        ROOT / "results/metrics.json",
        {
            "origin": "SCRIPTED_AND_MOCK_USAGE_ONLY",
            "rows": metrics,
            "quality": "NOT_EVALUABLE",
            "comparisons": [
                "B-A observation overhead",
                "C-B notice behavior and token increment",
                "C-A full mechanism net cost",
            ],
            "no_scalar_net_benefit_without_quality_and_actual_prices": True,
        },
    )
    # First-pass reviewer packet excludes detector outcomes, arm names, and preferred labels.
    tasks = json.loads((ROOT / "frozen/tasks.json").read_text(encoding="utf-8"))
    for task in tasks:
        source_path = ROOT / "seeds" / task["task_id"] / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        write(
            ROOT / "reviews/packets" / (task["task_id"] + ".json"),
            {
                "task_id": task["task_id"],
                "user_task": task["user_task"],
                "version": task["version"],
                "history_source_sha256": sha(source_path),
                "operations": source["operations"],
                "jobs": source["jobs"],
                "artifacts": source["artifacts"],
                "seed_messages": source["messages"],
                "execution_conditions": source["policy"]["profiles"],
                "questions": [
                    "What tests are required after the task boundary?",
                    "When can existing evidence satisfy the task?",
                    "Which repeated tests add information?",
                    "Which alternative paths are acceptable?",
                    "Can necessity still be tested after a passive notice arrives?",
                    "Is this fixture too simple to evaluate autonomy?",
                ],
                "status": "PENDING_INDEPENDENT_REVIEW",
            },
        )
    write(
        ROOT / "frozen/dependencies.json",
        {
            d.metadata["Name"]: d.version
            for d in importlib.metadata.distributions()
            if d.metadata["Name"]
        },
    )
    write(
        ROOT / "frozen/production-sources.json",
        {
            p.relative_to(REPO).as_posix(): sha(p)
            for base in (REPO / "src", REPO / "executor/src")
            for p in sorted(base.rglob("*.py"))
        },
    )
    write(ROOT / "results/preservation.json", preservation())
    counts = Counter((r["task"], r["arm"]) for r in metrics)
    write(
        ROOT / "results/run-counts.json", {f"{task}-{arm}": n for (task, arm), n in counts.items()}
    )
    # Readable ledger JSON is authoritative for inspection; SQLite is retained as raw audit.
    for path in ROOT.rglob("*.sqlite"):
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def seal():
    paths = [
        p
        for p in ROOT.rglob("*")
        if p.is_file()
        and p.name not in {"seal.json", "delivery.json"}
        and "__pycache__" not in p.parts
        and not p.name.endswith(("-wal", "-shm"))
    ]
    paths.extend(
        [
            REPO / "reports/history-aware-action-reflection-v1-pilot-readiness.md",
            REPO / "tests/experiments/test_pilot_readiness.py",
            REPO / "uv.lock",
            REPO / "pyproject.toml",
        ]
    )
    write(
        ROOT / "seal.json",
        {
            "stage": 6,
            "role": "POST_OFFLINE_PRE_LIVE_NOT_INDEPENDENT_EVALUATION",
            "files": {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(paths)},
            "provider_requests": 0,
            "admission": "BLOCKED",
        },
    )
    write(
        ROOT / "delivery.json",
        {
            "seal_sha256": sha(ROOT / "seal.json"),
            "admission": "BLOCKED",
            "report": "reports/history-aware-action-reflection-v1-pilot-readiness.md",
        },
    )


if __name__ == "__main__":
    import sys

    seal() if "--seal" in sys.argv else main()
