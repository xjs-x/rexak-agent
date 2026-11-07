"""固定 commit 与当前工作树分别计量，私有记忆和凭据不参与指纹。"""

from __future__ import annotations

import hashlib
import subprocess

from agent_workbench.maintenance.provenance import exporter_fingerprint, fingerprint_source


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def test_commit_fingerprint_survives_worktree_changes(tmp_path):
    _git(tmp_path, "init")
    (tmp_path / "uv.lock").write_bytes(b"version = 1\n")
    (tmp_path / "main.py").write_text("print('中文')\n", encoding="utf-8", newline="\n")
    (tmp_path / ".gitignore").write_text(".env\ndata/\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "baseline",
    )
    baseline = fingerprint_source(tmp_path)
    assert not baseline["dirty"]
    assert baseline["status"] == "available"
    assert all(
        set(entry) == {"path", "mode", "byte_size", "sha256"}
        for entry in baseline["committed_files"]
    )
    (tmp_path / "main.py").write_text("print('修改')\n", encoding="utf-8", newline="\n")
    (tmp_path / "new.py").write_bytes(b"x = 1\n")
    (tmp_path / ".env").write_bytes(b"API_KEY=private")
    (tmp_path / ".workbuddy").mkdir()
    (tmp_path / ".workbuddy" / "MEMORY.md").write_bytes(b"private memory")
    changed = fingerprint_source(tmp_path)
    assert changed["dirty"]
    assert changed["source_commit"] == baseline["source_commit"]
    assert changed["committed_tree_hash"] == baseline["committed_tree_hash"]
    assert changed["source_tree_hash"] != baseline["source_tree_hash"]
    assert changed["committed_lock_hash"] == hashlib.sha256(b"version = 1\n").hexdigest()
    assert {entry["path"] for entry in changed["working_files"]} == {
        ".gitignore",
        "main.py",
        "new.py",
        "uv.lock",
    }


def test_exporter_fingerprint_records_missing_git_without_blocking_evidence(tmp_path):
    result = exporter_fingerprint(tmp_path)
    assert result["status"] == "unavailable"
    assert result["error_code"] == "CalledProcessError"
    assert result["scope"] == "exporter_source_only_not_runtime_source"


def test_runtime_scope_skips_archives_and_tracks_uncommitted_source(tmp_path, monkeypatch):
    _git(tmp_path, "init")
    for name in ("src/main.py", "reports/large.zip", "experiments/run.json", "uv.lock"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"initial\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@invalid",
         "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    from pathlib import Path

    original_read = Path.read_bytes

    def guarded_read(path):
        assert not path.is_relative_to(tmp_path / "reports")
        assert not path.is_relative_to(tmp_path / "experiments")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    baseline = fingerprint_source(tmp_path, runtime_only=True)
    assert {item["path"] for item in baseline["working_files"]} == {"src/main.py", "uv.lock"}
    assert {item["path"] for item in baseline["committed_files"]} == {"src/main.py", "uv.lock"}
    (tmp_path / "reports/large.zip").write_bytes(b"archive changed")
    unchanged = fingerprint_source(tmp_path, runtime_only=True)
    assert unchanged["source_tree_hash"] == baseline["source_tree_hash"]
    assert not unchanged["dirty"]
    (tmp_path / "src/main.py").write_bytes(b"changed\n")
    (tmp_path / "src/new.py").write_bytes(b"new\n")
    changed = fingerprint_source(tmp_path, runtime_only=True)
    assert changed["dirty"]
    assert changed["source_tree_hash"] != baseline["source_tree_hash"]
    assert changed["committed_tree_hash"] == baseline["committed_tree_hash"]
    assert changed["hash_scope"] == "runtime_implementation_pathspec_v1"
