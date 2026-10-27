"""只读核验最终实现、原冻结评价与修复前源码；不重跑任何评价或业务调用。"""

from __future__ import annotations

import json
from pathlib import Path

from experiments.history_action_trusted_shadow_v1.evaluate import REPO, ROOT, preserved, sha


def verify():
    current = json.loads((ROOT / "delivery-code-freeze.json").read_text(encoding="utf-8"))
    for path, expected in current["files"].items():
        if sha(REPO / path) != expected:
            raise ValueError("delivery_source_drift:" + path)
    for path, expected in current["dependencies"].items():
        if sha(Path(path)) != expected:
            raise ValueError("dependency_drift:" + path)
    original = json.loads((ROOT / "freeze.json").read_text(encoding="utf-8"))
    plan = json.loads((ROOT / "post-evaluation-fix-plan.json").read_text(encoding="utf-8"))
    if sha(ROOT / "freeze.json") != plan["original_evaluation_freeze_sha256"]:
        raise ValueError("original_freeze_changed")
    for path, expected in original["files"].items():
        source = (
            ROOT / "evaluation-source" / Path(path).name
            if path in plan["original_sources"]
            else REPO / path
        )
        if sha(source) != expected:
            raise ValueError("original_evaluation_source_unavailable:" + path)
    for path, expected in original["loaded_modules"].items():
        source = Path(path)
        try:
            relative = source.relative_to(REPO).as_posix()
        except ValueError:
            relative = ""
        if relative in plan["original_sources"]:
            source = ROOT / "evaluation-source" / source.name
        if sha(source) != expected:
            raise ValueError("original_loaded_module_unavailable:" + path)
    return {
        "original_evaluation_inputs_labels_and_source": "PRESERVED_WITH_EXACT_SOURCE_ARCHIVES",
        "delivery_code_freeze_sha256": sha(ROOT / "delivery-code-freeze.json"),
        "preservation": preserved(),
        "formal_evaluation_rerun": False,
    }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False))
