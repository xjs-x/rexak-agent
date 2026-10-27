"""预览/密度分析/封存/单条授权执行。默认 verify，绝不自动跑全部场景。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from experiments.decision_memory_live_v1.harness import BUNDLE, ROOT, load, write_json

REPO = ROOT.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def density():
    result = []
    for number in (14, 15, 17):
        document = json.loads(
            (REPO / f"experiments/decision_memory_v1/paired-{number}.json").read_text(
                encoding="utf-8"
            )
        )
        c = next(row for row in document["rows"] if row["arm"] == "C")
        board = c["manifests"][0]["decision_memory"]["projection"]
        rendered = json.dumps(board, ensure_ascii=False)
        parts = {
            key: len(json.dumps(key, ensure_ascii=False))
            + 2
            + len(json.dumps(value, ensure_ascii=False))
            + (2 if i else 0)
            for i, (key, value) in enumerate(board.items())
        }
        assert sum(parts.values()) + 2 == len(rendered)
        refs = re.findall(r"(?:operation|artifact|job):[0-9a-f-]{36}", rendered)
        counts = Counter(refs)
        limitation = "仅比较已记录维度，不证明测试等价或前提已满足。"
        diffs = [d for group in board["condition_differences"].values() for d in group]
        labels = {14: {"source-A"}, 15: {"source-B", "source-K"}, 17: {"old-call", "source-C"}}[
            number
        ]
        core = [
            u for u in board["relevant_evidence"] if u["statement"].get("tool_call_id") in labels
        ]
        row = {
            "scenario": number,
            "source_file_sha256": sha(
                REPO / f"experiments/decision_memory_v1/paired-{number}.json"
            ),
            "system_chars": len(str(c["requests"][0][0]["content"])),
            "board_chars": len(rendered),
            "non_board_chars": len(str(c["requests"][0][0]["content"])) - len(rendered),
            "exact_field_chars": parts,
            "json_container_chars": 2,
            "compact_json_chars": len(json.dumps(board, ensure_ascii=False, separators=(",", ":"))),
            "reference_occurrences": len(refs),
            "unique_references": len(counts),
            "repeated_reference_occurrences": sum(n - 1 for n in counts.values()),
            "repeated_reference_chars": sum((n - 1) * len(ref) for ref, n in counts.items()),
            "condition_entries": len(diffs),
            "condition_statuses": dict(Counter(d["status"] for d in diffs)),
            "both_values_null_conditions": sum(
                d["old_value"] is None and d["new_value"] is None for d in diffs
            ),
            "repeated_limitation_count": rendered.count(limitation),
            "repeated_limitation_text_chars": rendered.count(limitation) * len(limitation),
            "selected_operations": len(board["relevant_evidence"]),
            "oracle_minimal_operation_units": len(core),
            "oracle_minimal_evidence_unit_chars": sum(
                len(json.dumps(u, ensure_ascii=False)) for u in core
            ),
            "other_evidence_unit_chars": sum(
                len(json.dumps(u, ensure_ascii=False))
                for u in board["relevant_evidence"]
                if u not in core
            ),
            "interpretation": "重复引用不是全部可删除；非最小Oracle集合不等于无用。这里只诊断序列化和闭包展开，不裁剪、不改生产选择器。",
        }
        result.append(row)
    write_json(ROOT / "preflight/density.json", result)
    return result


def source_files():
    files = [
        p
        for root in ("src/agent_workbench", "executor/src", "migrations")
        for p in (REPO / root).rglob("*")
        if p.is_file() and "__pycache__" not in str(p)
    ]
    files.extend(
        [
            Path(__file__),
            ROOT / "harness.py",
            REPO / "tests/experiments/test_decision_memory_live_preparation.py",
            REPO / "tests/experiments/decision_memory_benchmark.py",
            REPO / "tests/experiments/test_decision_memory_offline.py",
            REPO / "tests/integration/test_decision_memory.py",
            REPO / "tests/conftest.py",
            REPO / "uv.lock",
        ]
    )
    return sorted(set(files))


def seal():
    if (ROOT / "freeze.json").exists():
        raise RuntimeError("already_sealed_no_overwrite")
    required = [
        BUNDLE / "history.json",
        BUNDLE / "oracle.json",
        BUNDLE / "tools.json",
        BUNDLE / "config.json",
        BUNDLE / "simulator.json",
        BUNDLE / "scenarios.json",
    ] + [BUNDLE / f"initial/{case}.json" for case in load("config.json")["case_order"]]
    if any(not p.is_file() for p in required):
        raise RuntimeError("candidate_inputs_incomplete")
    acceptance = ROOT / "preflight/acceptance.json"
    if (
        not acceptance.exists()
        or json.loads(acceptance.read_text(encoding="utf-8")).get("status") != "MOCK_PREFLIGHT_PASS"
    ):
        raise RuntimeError("mock_preflight_not_accepted")
    files = {
        str(p.relative_to(REPO)).replace("\\", "/"): sha(p)
        for p in sorted(BUNDLE.rglob("*"))
        if p.is_file()
    }
    sources = {str(p.relative_to(REPO)).replace("\\", "/"): sha(p) for p in source_files()}
    write_json(
        ROOT / "freeze.json",
        {
            "version": 1,
            "status": "FROZEN_NOT_AUTHORIZED",
            "files": files,
            "source_files": sources,
            "approval_required": True,
            "max_total_provider_requests": 66,
        },
        exclusive=True,
    )
    return verify()


def verify():
    path = ROOT / "freeze.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for group in ("files", "source_files"):
        for name, expected in document[group].items():
            if sha(REPO / name) != expected:
                raise RuntimeError(f"freeze_mismatch:{name}")
    from importlib.metadata import version

    for package, expected in load("environment.json")["packages"].items():
        if version(package) != expected:
            raise RuntimeError(f"installed_dependency_mismatch:{package}")
    return sha(path)


def launch(case, approved_hash, request_cap):
    frozen_hash = verify()
    cfg = load("config.json")
    cap = 6 if case.endswith("C") else 8
    if approved_hash != frozen_hash or request_cap != cap or case not in cfg["case_order"]:
        raise RuntimeError("explicit_case_hash_and_exact_request_budget_required")
    if not os.environ.get("REXAK_MEMORY_LIVE_API_KEY"):
        raise RuntimeError("experiment_only_key_required_no_secret_store_fallback")
    results = ROOT / "live-results"
    results.mkdir(exist_ok=True)
    if (results / "STOP.json").exists():
        raise RuntimeError("stopped_experiment_requires_review_no_automatic_continuation")
    # 首批只允许 S14-C；其余格须另行显式命令，且首条协议通过后才解锁。
    if case != "S14-C":
        pilot = results / "S14-C.json"
        if (
            not pilot.exists()
            or json.loads(pilot.read_text(encoding="utf-8"))["status"] != "completed"
        ):
            raise RuntimeError("pilot_not_completed")
        readiness = json.loads(pilot.read_text(encoding="utf-8"))
        if not readiness.get("tool_loop_exercised") or not readiness.get(
            "artifact_body_cycle_exercised"
        ):
            raise RuntimeError("pilot_tool_or_body_cycle_not_exercised_natural_end_preserved")
    ledger = results / "ledger.jsonl"
    if (results / f"{case}.json").exists() or (
        ledger.exists()
        and any(
            json.loads(line).get("case") == case
            for line in ledger.read_text(encoding="utf-8").splitlines()
        )
    ):
        raise RuntimeError("case_already_attempted_no_automatic_rerun")
    lock = results / "execution.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    try:
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "REXAK_MEMORY_APPROVED_FREEZE": frozen_hash,
            "REXAK_MEMORY_LIVE_CASE": case,
        }
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/experiments/test_decision_memory_live_preparation.py::test_authorized_single_case",
                "-q",
                "--tb=no",
            ],
            cwd=REPO,
            env=env,
            check=False,
        ).returncode
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("density", "seal", "verify", "execute"), nargs="?", default="verify"
    )
    parser.add_argument("--case", choices=[f"S{n}-{a}" for n in (14, 15, 17) for a in "ABC"])
    parser.add_argument("--approved-freeze-sha256")
    parser.add_argument("--max-provider-requests", type=int)
    args = parser.parse_args()
    if args.command == "density":
        print(json.dumps(density(), ensure_ascii=False, indent=2))
    elif args.command == "seal":
        print(seal())
    elif args.command == "verify":
        print(verify())
    else:
        if not args.case:
            parser.error("execute requires one --case")
        return launch(args.case, args.approved_freeze_sha256, args.max_provider_requests)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
