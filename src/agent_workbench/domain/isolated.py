"""一次性本地解析任务。资源保护不是操作系统安全沙箱。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil


def run_isolated(
    module: str,
    path: Path,
    arguments: list[str],
    *,
    tmp_root: Path,
    seconds: float,
    memory_mb: int,
    output_bytes: int,
    process_factory: Any = None,
    monitor_factory: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """模块与路径仅由服务端决定；不使用 shell，不继承业务凭据。"""
    process_factory = process_factory or subprocess.Popen
    monitor_factory = monitor_factory or psutil.Process
    clock = clock or time.monotonic
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isolated-parse-", dir=tmp_root) as folder:
        output = Path(folder) / "result.json"
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "PATH", "TEMP", "TMP", "WINDIR"}
        }
        env["PYTHONIOENCODING"] = "utf-8"
        args = [sys.executable, "-m", module, str(path), str(output), *arguments]
        try:
            with process_factory(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
            ) as proc:
                started = clock()
                try:
                    monitor = monitor_factory(proc.pid)
                    while proc.poll() is None:
                        if clock() - started > seconds:
                            raise TimeoutError("parse_timeout")
                        try:
                            if monitor.memory_info().rss > memory_mb * 1024 * 1024:
                                raise MemoryError("parse_memory_limit")
                        except psutil.NoSuchProcess:
                            break
                        if output.exists() and output.stat().st_size > output_bytes:
                            raise ValueError("parser_output_limit")
                        time.sleep(0.025)
                    if (
                        proc.wait() != 0
                        or not output.is_file()
                        or output.stat().st_size > output_bytes
                    ):
                        raise ValueError("parser_output_limit_or_crash")
                    result = json.loads(output.read_text(encoding="utf-8"))
                    if not isinstance(result, dict):
                        raise ValueError("parser_invalid_output")
                    return result
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
        except (OSError, TimeoutError, MemoryError, ValueError) as exc:
            return {"status": "failed", "error_type": type(exc).__name__}
