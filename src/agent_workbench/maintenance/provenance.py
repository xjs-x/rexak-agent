"""源码指纹只描述被读取的树；导出机的代码不能冒充历史 run 的代码。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=True, timeout=30
    ).stdout


def _included(path: str) -> bool:
    return not path.startswith((".workbuddy/", ".env")) or path == ".env.example"


def _entry(path: str, mode: str, body: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "mode": mode,
        "byte_size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _tree_hash(entries: list[dict[str, Any]]) -> str:
    body = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def fingerprint_source(root: Path, *, ref: str = "HEAD") -> dict[str, Any]:
    """固定 commit 内容哈希，并单独记录当前工作树；不读取凭据正文。"""
    commit = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii").strip()
    listing = _git(root, "ls-tree", "-r", "-z", commit)
    modes: dict[str, str] = {}
    objects: list[tuple[str, str]] = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if kind != "blob":
            raise ValueError(f"源码指纹暂不支持子模块：{path}")
        if _included(path):
            modes[path] = mode
            objects.append((path, object_id))
    committed = []
    # cat-file 读取真实 blob；git archive 会受换行转换和 export 属性影响。
    batch = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input="".join(f"{identifier}\n" for _, identifier in objects).encode("ascii"),
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    position = 0
    for path, identifier in objects:
        end = batch.index(b"\n", position)
        object_id, kind, size_text = batch[position:end].decode("ascii").split()
        size = int(size_text)
        start = end + 1
        body = batch[start : start + size]
        if object_id != identifier or kind != "blob" or len(body) != size:
            raise ValueError(f"源码 blob 读取不完整：{path}")
        committed.append(_entry(path, modes[path], body))
        position = start + size + 1
    paths = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    working = []
    for name in sorted(set(paths.decode("utf-8").split("\0")) - {""}):
        if not _included(name):
            continue
        path = root / name
        if path.is_symlink():
            body = str(path.readlink()).replace("\\", "/").encode("utf-8")
            mode = "120000"
        elif path.is_file():
            body = path.read_bytes()
            mode = modes.get(name, "100644")
        else:
            continue
        working.append(_entry(name, mode, body))
    committed.sort(key=lambda item: item["path"])
    lock = next((item["sha256"] for item in committed if item["path"] == "uv.lock"), None)
    current_lock = next((item["sha256"] for item in working if item["path"] == "uv.lock"), None)
    status = _git(root, "status", "--porcelain=v1", "-z").decode("utf-8").split("\0")
    changes = []
    index = 0
    while index < len(status):
        record = status[index]
        index += 1
        if not record:
            continue
        names = [record[3:]]
        if "R" in record[:2] or "C" in record[:2]:
            names.append(status[index])
            index += 1
        if any(_included(name) for name in names):
            changes.append({"status": record[:2], "paths": names})
    return {
        "status": "available",
        "working_tree_consistency": "non_atomic_file_reads",
        "source_commit": commit,
        "committed_tree_hash": _tree_hash(committed),
        "committed_lock_hash": lock,
        "source_tree_hash": _tree_hash(working),
        "lock_hash": current_lock,
        "dirty": bool(changes),
        "worktree_changes": changes,
        "raw_content_differs_from_commit": committed != working,
        "hash_method": "sha256_sorted_path_mode_size_content_sha256_v1",
        "hash_scope": "tracked_and_unignored_files_except_workbuddy_and_local_env",
        "committed_files": committed,
        "working_files": working,
    }


def exporter_fingerprint(root: Path) -> dict[str, Any]:
    """安装包或 Git 不可用时显式记录缺失，仍允许导出真实运行证据。"""
    try:
        return fingerprint_source(root)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return {
            "status": "unavailable",
            "error_code": type(exc).__name__,
            "scope": "exporter_source_only_not_runtime_source",
        }
