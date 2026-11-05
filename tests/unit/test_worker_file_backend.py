"""Remote executable resolution stays inside the Executor installation, never Node paths."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from workbench_executor import worker_files
from workbench_executor.command_runtime import CommandRuntime, RuntimeRejected
from workbench_executor.settings import ExecutorSettings


def test_rg_prefers_installed_system_binary(monkeypatch):
    monkeypatch.setattr(worker_files.shutil, "which", lambda _: "/usr/bin/rg")
    assert worker_files.rg_executable() == "/usr/bin/rg"


def test_rg_supports_nonroot_executor_local_install(monkeypatch):
    monkeypatch.setattr(worker_files.shutil, "which", lambda _: None)
    monkeypatch.setattr(worker_files.sys, "executable", "/executor/.venv/bin/python")
    assert worker_files.rg_executable() == str(Path("/executor/.venv/bin/rg"))


def test_worker_default_profiles_and_temp_are_separate_and_identity_bound(tmp_path):
    runtime = CommandRuntime(ExecutorSettings(data_root=tmp_path, workspace_root=tmp_path / "workspaces"), None, "fixture")
    session, run, operation, first, second = [str(uuid4()) for _ in range(5)]

    def spec(worker=None, envelope_worker=None):
        envelope = cast(Any, SimpleNamespace(session_id=session, run_id=run, operation_id=operation,
            delegation_id=envelope_worker or worker, delegation_revision=1))
        args = {"argv": ["true"], "workspace_id": session}
        if worker:
            args["worker_workspace_id"] = worker
        return runtime._build_spec(envelope, args, "command", str(uuid4()), "fixture", 1024)

    a, b, main = spec(first), spec(second), spec()
    assert "HOME" not in main["env"] and "TMPDIR" not in main["env"]
    for name in ["HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"]:
        assert a["env"][name] != b["env"][name]
        assert Path(a["env"][name]).is_relative_to(Path(a["cwd"]))
    with pytest.raises(RuntimeRejected):
        spec(first, second)


def test_helper_uses_isolated_import_and_cannot_load_cwd_modules(tmp_path):
    import json
    import subprocess
    import sys

    marker = tmp_path / "IMPORTED"
    (tmp_path / "json.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()", encoding="utf-8")
    package = tmp_path / "workbench_executor"
    package.mkdir()
    (package / "__init__.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()", encoding="utf-8")
    (tmp_path / "note.txt").write_text("safe", encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-m", "workbench_executor.worker_files"], cwd=tmp_path,
        input=json.dumps({"action": "read", "path": "note.txt"}).encode(), capture_output=True, timeout=15)
    assert result.returncode == 0 and json.loads(result.stdout)["ok"] and not marker.exists()


def test_search_limits_count_match_events_and_keep_complete_utf8_records(tmp_path, monkeypatch):
    import io
    import json

    class Process:
        stdout = io.BytesIO()
        def terminate(self):
            pass
        def wait(self, **_kwargs):
            return 0

    calls = []
    def popen(args, **kwargs):
        calls.append(args)
        process = Process()
        if "--json" in args:
            events = [{"type": kind, "data": {"path": {"text": "./note.txt"}, "lines": {"text": text}, "line_number": i}}
                for i, kind, text in [(1, "context", "before\n"), (2, "match", "needle\n"), (3, "context", "after\n"), (4, "match", "needle again\n")]]
            process.stdout = io.BytesIO(("\n".join(json.dumps(event) for event in events)+"\n").encode())
        else:
            process.stdout = io.BytesIO(b"./note.txt\n" + b"a"*65523 + "你".encode()*3)
        return process
    (tmp_path / "note.txt").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(worker_files.subprocess, "Popen", popen)
    found = worker_files.perform({"action": "grep", "pattern": "needle", "context": 2, "limit": 1}, tmp_path)
    assert "note.txt:2:needle" in found["lines"] and not any(":4:" in line for line in found["lines"])
    assert found["truncated"] and "--hidden" in calls[0]
    paths = worker_files.perform({"action": "glob", "pattern": "*"}, tmp_path)
    assert paths["lines"] == ["note.txt"] and paths["truncated"]
