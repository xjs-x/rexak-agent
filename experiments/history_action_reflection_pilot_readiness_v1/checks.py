"""Run explicit local checks with UTF-8 logs; immutable per-attempt filenames."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .protocol import REPO, ROOT, write


def main():
    commands = {
        "new-pytest": [
            "uv",
            "run",
            "pytest",
            "tests/experiments/test_pilot_readiness.py",
            "-q",
            "--junitxml=" + str(ROOT / "checks/new-tests.xml"),
        ],
        "related-pytest": [
            "uv",
            "run",
            "pytest",
            "tests/experiments/test_reflection_preparation.py",
            "tests/unit/test_history_action_reflection.py",
            "tests/unit/test_evidence_action_evaluator.py",
            "tests/unit/test_evidence_action_reliability.py",
            "tests/experiments/test_autonomous_shadow.py",
            "-q",
        ],
        "ruff-new": [
            "uv",
            "run",
            "ruff",
            "check",
            str(ROOT),
            "tests/experiments/test_pilot_readiness.py",
        ],
        "ruff-all": ["uv", "run", "ruff", "check", ".", "--output-format", "concise"],
        "pyright-new": [
            "uv",
            "run",
            "pyright",
            str(ROOT),
            "tests/experiments/test_pilot_readiness.py",
        ],
        "pyright-default": ["uv", "run", "pyright"],
        "encoding": ["uv", "run", "python", "scripts/check_encoding.py"],
        "encoding-new": [
            "uv",
            "run",
            "python",
            "scripts/check_encoding.py",
            "--root",
            str(ROOT),
            "--root",
            "reports",
        ],
    }

    def run(item):
        name, command = item
        result = subprocess.run(
            command,
            cwd=REPO,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            timeout=300,
        )
        text = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
        target = ROOT / "checks" / (name + ".log")
        target.parent.mkdir(exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return {"name": name, "command": command, "exit_code": result.returncode}

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, commands.items()))
    write(ROOT / "checks/results.json", results)
    for row in results:
        print(row["name"], row["exit_code"])


if __name__ == "__main__":
    main()
