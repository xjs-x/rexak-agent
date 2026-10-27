"""只读校验本轮来源和历史保全；freeze 只封存离线开发完成版本，不授权真实调用。"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

from experiments.history_action_reflection_preparation_v1.benchmark import ROOT, write
from experiments.history_action_reflection_preparation_v1.prepare import REPO, sha

ALLOWED = {"src/agent_workbench/tools/history_action_shadow.py",
           "src/agent_workbench/tools/shadow_storage.py",
           "src/agent_workbench/domain/trusted_http_shadow.py"}


def preservation():
    baseline = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    changed = [p for p, h in baseline.items() if not (REPO / p).is_file() or sha(REPO / p) != h]
    if set(changed) - ALLOWED:
        raise ValueError("unapproved_existing_bytes_changed:" + str(changed))
    for name in ALLOWED:
        if sha(ROOT / "baseline" / name) != baseline[name]:
            raise ValueError("original_source_archive_changed")
    return {"baseline_files": len(baseline), "changed": changed, "unchanged": len(baseline) - len(changed)}


def input_sources():
    source = json.loads((ROOT / "prepared/source-manifest.json").read_text(encoding="utf-8"))
    for name, digest in source["source_files"].items():
        if sha(ROOT / "prepared/producer-sources" / Path(name).name) != digest:
            raise ValueError("prepared_producer_source_mismatch:" + name)
    for name, digest in source["files"].items():
        if sha(ROOT / "prepared" / name) != digest:
            raise ValueError("prepared_input_or_label_changed:" + name)
    seal = json.loads((ROOT / "review-set/seal.json").read_text(encoding="utf-8"))
    for name, digest in seal["files"].items():
        if sha(ROOT / "review-set" / name) != digest:
            raise ValueError("review_seed_seal_changed")
    return {"prepared_inputs": "MATCHED", "producer_original_bytes": "MATCHED",
            "review_seeds": "MATCHED", "independent_labels": "NOT_AVAILABLE"}


def freeze():
    source_paths = [*ROOT.glob("*.py"), *ROOT.glob("baseline/**/*.py"), *ROOT.glob("prepared/**/*.py"),
                    *ROOT.glob("prepared/*.json"), *ROOT.glob("review-set/*.json"),
                    ROOT / "protocol.json", ROOT / "performance.json", ROOT / "offline-results-v2.json",
                    ROOT / "task-link-results.json", ROOT / "lifecycle-diagnostic.json",
                    *(REPO / p for p in ALLOWED),
                    REPO / "tests/experiments/test_reflection_preparation.py",
                    REPO / "tests/integration/test_reflection_preparation.py",
                    REPO / "src/agent_workbench/domain/history_action_reflection.py",
                    REPO / "src/agent_workbench/domain/shadow_authorization.py",
                    REPO / "src/agent_workbench/tools/http.py", REPO / "src/agent_workbench/runtime/runner.py",
                    REPO / "src/agent_workbench/agent/middleware/tool_dispatch.py", REPO / "uv.lock"]
    write(ROOT / "freeze-final.json", {
        "role": "POST_OFFLINE_DEVELOPMENT_PRE_REAL_PROVIDER_PREPARATION_NOT_LIVE_AUTHORIZATION",
        "real_provider_requests": 0, "production_feedback_enabled": False,
        "independence": "NO_INDEPENDENT_HUMAN_LABELS; review seeds are development, not heldout",
        "preservation": preservation(), "inputs": input_sources(), "python": sys.version,
        "packages": {n: importlib.metadata.version(n) for n in ("pydantic", "langchain", "langgraph", "sqlalchemy", "httpx", "pytest")},
        "files": {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(source_paths))},
    })
    print("freeze_sha256=" + sha(ROOT / "freeze-final.json"))


def verify():
    frozen = json.loads((ROOT / "freeze-final.json").read_text(encoding="utf-8"))
    for name, digest in frozen["files"].items():
        if sha(REPO / name) != digest:
            raise ValueError("final_freeze_mismatch:" + name)
    return {"freeze_sha256": sha(ROOT / "freeze-final.json"), "preservation": preservation(), **input_sources()}


if __name__ == "__main__":
    if sys.argv[1:] == ["freeze"]:
        freeze()
    else:
        print(json.dumps(verify(), ensure_ascii=False))
