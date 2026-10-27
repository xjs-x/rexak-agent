"""独立 A/B 冻结与单组执行；固定顺序，不执行既有 C 组。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from experiments.decision_memory_ab_comparison_v1 import harness
from experiments.decision_memory_live_v1.prepare import verify as verify_original
from experiments.decision_memory_s14c_r3.prepare import dependency_snapshot, sha, source_files

ROOT = harness.ROOT
REPO = ROOT.parents[1]
PARENT_HASH = "3585c387208e06f76257949ca8f7aeadd2d603ff6556f2726285d680c972df31"


def verify_preservation():
    parent = ROOT.parent / "decision_memory_s14c_r3/freeze.json"
    if sha(parent) != PARENT_HASH:
        raise RuntimeError("C_R3_freeze_changed")
    document = json.loads(parent.read_text(encoding="utf-8"))
    for group in ("files", "source_files"):
        for name, expected in document[group].items():
            if sha(REPO / name) != expected:
                raise RuntimeError("C_R3_frozen_material_changed:" + name)
    baseline = json.loads((ROOT / "preflight/preservation-before.json").read_text(encoding="utf-8"))
    for name, expected in baseline.items():
        if not (REPO / name).is_file() or sha(REPO / name) != expected:
            raise RuntimeError("preexisting_material_changed:" + name)
    return {"status": "PRESERVED", "checked_files": len(baseline), "changed_files": []}


def seal():
    if (ROOT / "freeze.json").exists():
        raise RuntimeError("already_sealed_no_overwrite")
    legacy_hash = verify_original()
    preservation = verify_preservation()
    for name in ("initial/A.json", "initial/B.json", "evaluation-contract.json", "oracle.json"):
        if not (ROOT / "bundle" / name).is_file():
            raise RuntimeError("required_frozen_input_missing:" + name)
    acceptance = json.loads((ROOT / "preflight/acceptance.json").read_text(encoding="utf-8"))
    if acceptance["status"] != "MOCK_PREFLIGHT_PASS":
        raise RuntimeError("mock_preflight_not_accepted")
    harness.original.write_json(
        ROOT / "bundle/identities.json",
        {a: harness.identity(a) for a in harness.ARMS},
        exclusive=True,
    )
    harness.original.write_json(
        ROOT / "bundle/dependencies.json", dependency_snapshot(), exclusive=True
    )
    files = {
        p.relative_to(REPO).as_posix(): sha(p)
        for folder in ("bundle", "preflight")
        for p in sorted((ROOT / folder).rglob("*"))
        if p.is_file()
    }
    harness.original.write_json(
        ROOT / "freeze.json",
        {
            "version": 1,
            "status": "FROZEN_SCOPE_AUTHORIZED",
            "canonical_root": "experiments/decision_memory_ab_comparison_v1",
            "canonical_root_absolute": str(ROOT.resolve()),
            "identities": {a: harness.identity(a) for a in harness.ARMS},
            "parent_C_R3_freeze_sha256": PARENT_HASH,
            "base_freeze_sha256": legacy_hash,
            "files": files,
            "source_files": source_files(),
            "preservation": preservation,
            "max_group_requests": 8,
            "max_agent_requests": 6,
            "max_graph_review_requests": 2,
            "max_total_new_requests": 16,
            "max_estimated_input_tokens": 131072,
            "execution_order": ["A", "B"],
            "C_is_read_only_existing_R3": True,
        },
        exclusive=True,
    )
    return verify()


def verify():
    frozen = json.loads((ROOT / "freeze.json").read_text(encoding="utf-8"))
    if (
        frozen["canonical_root"] != "experiments/decision_memory_ab_comparison_v1"
        or ROOT.resolve() != REPO / frozen["canonical_root"]
        or ROOT.resolve() != Path(frozen["canonical_root_absolute"]).resolve()
    ):
        raise RuntimeError("canonical_comparison_root_mismatch")
    if frozen["identities"] != {a: harness.identity(a) for a in harness.ARMS}:
        raise RuntimeError("comparison_identity_mismatch")
    if (
        frozen["base_freeze_sha256"] != verify_original()
        or frozen["parent_C_R3_freeze_sha256"] != PARENT_HASH
    ):
        raise RuntimeError("parent_freeze_mismatch")
    if frozen["source_files"] != source_files():
        raise RuntimeError("source_inventory_or_hash_mismatch")
    for name, expected in frozen["files"].items():
        if sha(REPO / name) != expected:
            raise RuntimeError("freeze_mismatch:" + name)
    import importlib.metadata

    dependencies = json.loads((ROOT / "bundle/dependencies.json").read_text(encoding="utf-8"))
    if dependencies["python"] != sys.version:
        raise RuntimeError("python_runtime_mismatch")
    for package, expected in dependencies["packages"].items():
        if importlib.metadata.version(package) != expected:
            raise RuntimeError("dependency_version_mismatch:" + package)
    for name, expected in dependencies["files"].items():
        if sha(Path(sys.prefix) / name) != expected:
            raise RuntimeError("dependency_file_mismatch:" + name)
    for name, expected in dependencies["runtime_files"].items():
        if sha(Path(name)) != expected:
            raise RuntimeError("runtime_file_mismatch")
    verify_preservation()
    return sha(ROOT / "freeze.json")


def launch(arm, approved_hash, cap):
    if approved_hash != verify() or cap != 8:
        raise RuntimeError("explicit_AB_hash_and_eight_requests_required")
    if not os.environ.get("REXAK_MEMORY_LIVE_API_KEY"):
        raise RuntimeError("experiment_only_key_required")
    if (ROOT / "INFRASTRUCTURE_STOP.json").exists():
        raise RuntimeError("shared_infrastructure_stopped")
    if arm == "B" and harness.audit_a_before_b()["transport_mode"] != "live":
        raise RuntimeError("B_requires_preserved_real_A_attempt")
    results = harness.results_for(arm)
    results.mkdir(parents=True, exist_ok=True)
    lock = ROOT / "live-results/execution.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    try:
        if any(
            (results / n).exists()
            for n in ("ATTEMPT.json", "STOP.json", "ledger.jsonl", "result.json")
        ):
            raise RuntimeError("iteration_already_attempted_no_replay")
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "REXAK_MEMORY_AB_APPROVED_FREEZE": approved_hash,
            "REXAK_MEMORY_AB_ARM": arm,
        }
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/experiments/test_decision_memory_ab_comparison_v1.py::test_authorized_baseline",
                "-q",
                "--tb=short",
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
        "command", choices=("seal", "verify", "execute"), nargs="?", default="verify"
    )
    parser.add_argument("--arm", choices=harness.ARMS)
    parser.add_argument("--approved-freeze-sha256")
    parser.add_argument("--max-provider-requests", type=int)
    args = parser.parse_args()
    if args.command == "execute":
        if args.arm is None:
            parser.error("execute requires one --arm")
        return launch(args.arm, args.approved_freeze_sha256, args.max_provider_requests)
    print(seal() if args.command == "seal" else verify())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
