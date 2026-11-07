"""执行环境和产物的只读投影；不推断漏洞是否成立。"""

from pathlib import Path
from typing import Any


def execution_context(spec: dict[str, Any]) -> dict[str, Any]:
    return {key: spec[key] for key in ("cwd", "workspace_root", "strict_shell") if key in spec}


def check_outputs(spec: dict[str, Any]) -> dict[str, Any]:
    results = []
    root = Path(spec.get("workspace_root") or spec["cwd"]).resolve()
    for name in spec.get("expected_files", []):
        state = "outside_workspace"
        size = None
        try:
            target = (Path(spec["cwd"]) / name).resolve()
            if target.is_relative_to(root):
                if target.is_file():
                    size = target.stat().st_size
                    state = "nonempty" if size else "empty"
                else:
                    state = "missing_or_not_file"
        except (OSError, RuntimeError):
            state = "unreadable"
        results.append({"path": name, "state": state, "byte_size": size})
    return {
        "output_validation": ("passed" if all(r["state"] == "nonempty" for r in results)
                              else "failed") if results else "not_requested",
        "files": results,
        "note": "仅核对声明文件存在且非空；不证明内容正确、新生成或漏洞成立。",
    }
