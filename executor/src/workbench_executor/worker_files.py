"""Fixed Worker file backend, executed as a normal fenced/durable command job in Kali."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from workbench_executor.filesystem import WorkspaceError, WorkspaceFileSystem

MAX_FILE = 4 * 1024 * 1024


def rg_executable() -> str:
    """Use system rg or the administrator-installed binary beside the Executor interpreter."""
    return shutil.which("rg") or str(Path(sys.executable).with_name("rg"))


def perform(value: dict[str, Any], root: Path) -> dict[str, Any]:
    fs = WorkspaceFileSystem(root, max_read_bytes=MAX_FILE, max_write_bytes=MAX_FILE)
    action, path = value["action"], str(value.get("path") or ".")
    if action in {"access", "exists"}:
        try:
            target = fs.resolve(path)
        except WorkspaceError as exc:
            if action == "exists" and exc.code == "not_found":
                return {"exists": False}
            raise
        return {"exists": True, "directory": target.is_dir()}
    if action == "read":
        target = fs.resolve(path)
        if not target.is_file() or target.stat().st_size > MAX_FILE:
            raise WorkspaceError("content_too_large", "Worker 文件不是常规小文件或超过 4 MiB")
        raw = target.read_bytes()
        if len(raw) > MAX_FILE:
            raise WorkspaceError("content_too_large", "Worker 文件超过 4 MiB")
        import hashlib

        return {"data_base64": base64.b64encode(raw).decode(), "version": hashlib.sha256(raw).hexdigest()}
    if action == "mkdir":
        target = fs.resolve(path, must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        fs.resolve(path)
        return {"created": True}
    if action == "write":
        # All helper writers serialize the version-check + atomic replace. Bash remains trusted execution.
        with (fs.root / ".worker-write.lock").open("a+b") as lock:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = fs.file_version(path)
            expected = value.get("expected_version", current)
            return fs.write_file(path, str(value["content"]), expected_version=expected)
    if action in {"glob", "grep"}:
        target = fs.resolve(path)
        limit = max(1, min(int(value.get("limit", 1000 if action == "glob" else 100)), 1000))
        if action == "glob":
            args = [rg_executable(), "--files", "--hidden", "--glob", str(value["pattern"]), *search_exclusions(), "."]
        else:
            args = [rg_executable(), "--json", "--hidden", *search_exclusions(), "--max-count", str(limit + 1)]
            if value.get("ignoreCase"):
                args.append("--ignore-case")
            if value.get("literal"):
                args.append("--fixed-strings")
            if value.get("glob"):
                args += ["--glob", str(value["glob"])]
            args += ["--context", str(max(0, min(int(value.get("context", 0)), 10))), "--", str(value["pattern"]), "." if target.is_dir() else target.name]
        # rg runs ONLY in Kali. Stream only a bounded prefix, then terminate and reap it.
        process = subprocess.Popen(args, cwd=target if target.is_dir() else target.parent, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert process.stdout
        raw = process.stdout.read(65537)
        clipped = len(raw) > 65536
        if clipped:
            process.terminate()
        code = process.wait(timeout=15)
        if code not in (0, 1) and not clipped:
            raise WorkspaceError("search_failed", f"rg 退出码 {code}")
        # Only complete records survive a byte cap; a partial UTF-8 filename/JSON
        # record is not an error in the already captured, valid search results.
        captured = raw[:65536]
        if clipped:
            captured = captured[:captured.rfind(b"\n") + 1]
        text = captured.decode("utf-8", errors="strict")
        lines = text.splitlines()
        # Never return symlink escapes as discoverable file paths.
        if action == "glob":
            valid = []
            for item in lines:
                try:
                    if fs.resolve(str(target.relative_to(fs.root) / item)).is_file():
                        valid.append(item.removeprefix("./"))
                except WorkspaceError:
                    clipped = True
            return {"lines": valid[:limit], "truncated": clipped or len(valid) > limit}
        events = [json.loads(line) for line in lines]
        matches = [e["data"] for e in events if e["type"] == "match"]
        selected = matches[:limit]
        context = max(0, min(int(value.get("context", 0)), 10))
        rendered = []
        for event in events:
            if event["type"] not in {"match", "context"}:
                continue
            data = event["data"]
            if event["type"] == "match" and data not in selected:
                continue
            if not any(data["path"] == m["path"] and abs(data["line_number"] - m["line_number"]) <= context for m in selected):
                continue
            path_text, line_text = data["path"].get("text"), data["lines"].get("text")
            if path_text is None or line_text is None:
                clipped = True
                continue
            separator = ":" if event["type"] == "match" else "-"
            rendered.append(f"{path_text.removeprefix('./')}{separator}{data['line_number']}{separator}{line_text.rstrip(chr(10))}")
        return {"lines": rendered, "truncated": clipped or len(matches) > limit}
    raise WorkspaceError("invalid_action", "未知 Worker 文件操作")


def search_exclusions() -> list[str]:
    return [part for pattern in (".git", "node_modules", ".home", ".tmp", ".worker-write.lock")
            for part in ("--glob", f"!{pattern}")]


def main() -> None:
    try:
        data = sys.stdin.buffer.read(MAX_FILE * 2 + 1)
        if len(data) > MAX_FILE * 2:
            raise WorkspaceError("content_too_large", "Worker 请求过大")
        result = {"ok": True, "data": perform(json.loads(data), Path.cwd())}
    except (WorkspaceError, ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"ok": False, "error": exc.code if isinstance(exc, WorkspaceError) else type(exc).__name__}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
