"""只读核验本轮交付、正式冻结和初始源码；不执行评价或连接数据库。"""

from __future__ import annotations

import json
from pathlib import Path

from experiments.history_action_autonomous_shadow_v1.evaluate import REPO, ROOT, sha, verify


def main():
    manifest = json.loads((ROOT / "delivery-manifest.json").read_text(encoding="utf-8"))
    for path, expected in manifest["files"].items():
        if sha(REPO / path) != expected:
            raise ValueError("delivery_material_drift:" + path)
    for path, expected in manifest.get("runtime_files", {}).items():
        if sha(Path(path)) != expected:
            raise ValueError("runtime_file_drift:" + path)
    before = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    count = 0
    for path, expected in before.items():
        if path.startswith("src/agent_workbench/") and path.endswith(".py"):
            if sha(ROOT / "baseline" / path) != expected:
                raise ValueError("initial_source_snapshot_drift:" + path)
            count += 1
    return {**verify(), "delivery_files": len(manifest["files"]), "initial_production_source_snapshots": count, "formal_evaluation_rerun": False}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
