"""备份完整性校验及 CLI 退出码。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_workbench.cli import _backup_file_entries


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_workbench.cli", "verify-backup", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def test_verify_backup_detects_corruption_and_missing_files(tmp_path: Path) -> None:
    dump = tmp_path / "database.sql"
    dump.write_text("SELECT '中文';\n", encoding="utf-8")
    artifact = tmp_path / "artifacts" / "a.txt"
    artifact.parent.mkdir()
    artifact.write_text("原始证据", encoding="utf-8")
    (tmp_path / "MANIFEST.json").write_text(
        json.dumps({"schema_version": 1, "files": _backup_file_entries(tmp_path)}),
        encoding="utf-8",
    )

    assert _verify(tmp_path).returncode == 0
    artifact.write_text("损坏证据", encoding="utf-8")
    assert _verify(tmp_path).returncode == 1
    artifact.unlink()
    assert _verify(tmp_path).returncode == 1
    assert _verify(tmp_path / "missing").returncode == 1
