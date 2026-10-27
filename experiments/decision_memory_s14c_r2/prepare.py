"""R2 离线封存/核验；execute 仅接受下一轮明确批准的独立冻结哈希。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

from experiments.decision_memory_live_v1.prepare import verify as verify_original
from experiments.decision_memory_s14c_r2 import harness

ROOT = harness.ROOT
REPO = ROOT.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    paths = {
        p
        for name in ("src", "executor/src", "migrations", "tests", "scripts")
        for p in (REPO / name).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    paths.update((REPO / "experiments").rglob("*.py"))
    paths.update(REPO / n for n in ("uv.lock", "pyproject.toml", ".python-version", "AGENTS.md"))
    return {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(paths)}


def dependency_snapshot():
    """冻结已安装分发包实际文件，包含运行时稍后导入的模块，而不只记版本。"""
    prefix = Path(sys.prefix).resolve()
    files, versions = {}, {}
    for distribution in importlib.metadata.distributions():
        versions[distribution.metadata["Name"]] = distribution.version
        for item in distribution.files or []:
            path = Path(str(distribution.locate_file(item))).resolve()
            if path.is_relative_to(prefix) and path.is_file() and path.suffix != ".pyc":
                files[path.relative_to(prefix).as_posix()] = sha(path)
    # Python 本体和已经加载的标准库文件单独记录。
    loaded = set()
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).is_file():
            loaded.add(Path(filename).resolve())
    runtime = {str(p): sha(p) for p in sorted(loaded) if not p.is_relative_to(REPO)}
    runtime[str(Path(sys.executable).resolve())] = sha(Path(sys.executable))
    return {"python": sys.version, "packages": versions, "files": files, "runtime_files": runtime}


def verify_preservation():
    baseline = json.loads((ROOT / "preflight/preservation-before.json").read_text(encoding="utf-8"))
    changes = [
        name
        for name, expected in baseline.items()
        if not (REPO / name).is_file() or sha(REPO / name) != expected
    ]
    if changes:
        raise RuntimeError("preexisting_material_changed:" + ",".join(changes))
    return {"checked_files": len(baseline), "changed_files": [], "status": "PRESERVED"}


def seal():
    if (ROOT / "freeze.json").exists():
        raise RuntimeError("already_sealed_no_overwrite")
    legacy_hash = verify_original()
    preservation = verify_preservation()
    candidate = ROOT / "bundle/initial.json"
    if not candidate.is_file():
        raise RuntimeError("mock_initial_request_required")
    acceptance = json.loads((ROOT / "preflight/acceptance.json").read_text(encoding="utf-8"))
    if acceptance.get("status") != "MOCK_PREFLIGHT_PASS":
        raise RuntimeError("mock_preflight_not_accepted")
    harness.original.write_json(ROOT / "bundle/identity.json", harness.identity(), exclusive=True)
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
            "version": 2,
            "status": "FROZEN_NOT_AUTHORIZED",
            "identity": harness.identity(),
            "canonical_root": ROOT.relative_to(REPO).as_posix(),
            "canonical_root_absolute": str(ROOT.resolve()),
            "base_freeze_sha256": legacy_hash,
            "files": files,
            "source_files": source_files(),
            "preservation": preservation,
            "approval_required": True,
            "max_provider_requests": 6,
            "campaign_total_cap": 66,
            "previous_reserved_requests": len(
                [
                    r
                    for r in harness.read_ledger(
                        harness.original.ROOT / "live-results/ledger.jsonl"
                    )
                    if r.get("record") == "reserved"
                ]
            ),
        },
        exclusive=True,
    )
    return verify()


def verify():
    path = ROOT / "freeze.json"
    frozen = json.loads(path.read_text(encoding="utf-8"))
    if (
        frozen["canonical_root"] != "experiments/decision_memory_s14c_r2"
        or ROOT.resolve() != REPO / frozen["canonical_root"]
        or ROOT.resolve() != Path(frozen["canonical_root_absolute"]).resolve()
    ):
        raise RuntimeError("canonical_iteration_root_mismatch")
    if frozen["identity"] != harness.identity():
        raise RuntimeError("iteration_identity_mismatch")
    if frozen["base_freeze_sha256"] != verify_original():
        raise RuntimeError("legacy_freeze_mismatch")
    if frozen["source_files"] != source_files():
        raise RuntimeError("source_inventory_or_hash_mismatch")
    for name, expected in frozen["files"].items():
        if sha(REPO / name) != expected:
            raise RuntimeError(f"freeze_mismatch:{name}")
    dependencies = json.loads((ROOT / "bundle/dependencies.json").read_text(encoding="utf-8"))
    if dependencies["python"] != sys.version:
        raise RuntimeError("python_runtime_mismatch")
    for package, expected in dependencies["packages"].items():
        if importlib.metadata.version(package) != expected:
            raise RuntimeError(f"dependency_version_mismatch:{package}")
    for name, expected in dependencies["files"].items():
        if sha(Path(sys.prefix) / name) != expected:
            raise RuntimeError(f"dependency_file_mismatch:{name}")
    for name, expected in dependencies["runtime_files"].items():
        if sha(Path(name)) != expected:
            raise RuntimeError("loaded_runtime_file_mismatch")
    verify_preservation()
    return sha(path)


def launch(approved_hash, cap):
    if approved_hash != verify() or cap != 6:
        raise RuntimeError("explicit_r2_hash_and_six_requests_required")
    if not os.environ.get("REXAK_MEMORY_LIVE_API_KEY"):
        raise RuntimeError("experiment_only_key_required")
    results = ROOT / "live-results"
    results.mkdir(exist_ok=True)
    lock = results / "execution.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    try:
        if any(
            (results / n).exists()
            for n in ("ATTEMPT.json", "ledger.jsonl", "STOP.json", f"{harness.CASE}.json")
        ):
            raise RuntimeError("iteration_already_attempted_no_replay")
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "REXAK_MEMORY_R2_APPROVED_FREEZE": approved_hash,
        }
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/experiments/test_decision_memory_s14c_r2.py::test_authorized_r2",
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
    parser.add_argument("--approved-freeze-sha256")
    parser.add_argument("--max-provider-requests", type=int)
    args = parser.parse_args()
    if args.command == "execute":
        return launch(args.approved_freeze_sha256, args.max_provider_requests)
    print(seal() if args.command == "seal" else verify())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
