"""个人部署的 Executor 进程管理；仅管理本目录启动的进程。"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
from workbench_executor.processes import read_pid_start_ticks
from workbench_executor.settings import get_executor_settings

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / "executor.pid"
IDENTITY_FILE = ROOT / "executor-launch.json"


class ProcessIdentityMismatch(RuntimeError):
    """旧 PID 指向其他进程；停止时拒绝操作，启动时只忽略旧记录。"""


def owned_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return None
    if (proc / "cwd").resolve() != ROOT:
        raise ProcessIdentityMismatch("PID 文件指向其他工作目录，拒绝停止")
    argv = (proc / "cmdline").read_bytes().split(b"\0")
    if b"-m" not in argv or b"workbench_executor" not in argv:
        raise ProcessIdentityMismatch("PID 文件指向其他程序，拒绝停止")
    if IDENTITY_FILE.exists():
        identity = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
        if identity["pid"] != pid or identity["start_ticks"] != read_pid_start_ticks(pid):
            raise ProcessIdentityMismatch("进程身份已变化，拒绝停止")
    return pid


def healthy() -> bool:
    settings = get_executor_settings()
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.get(f"http://127.0.0.1:{settings.port}/health")
            response.raise_for_status()
            return response.json().get("executor_id") == settings.executor_id
    except (httpx.HTTPError, ValueError):
        return False


def stop() -> None:
    pid = owned_pid()
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            proc = Path(f"/proc/{pid}")
            if not proc.exists() or proc.joinpath("stat").read_text().split(") ", 1)[1].startswith(
                "Z"
            ):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Executor 未在 30 秒内退出；保留身份记录，不强制结束")
    PID_FILE.unlink(missing_ok=True)
    IDENTITY_FILE.unlink(missing_ok=True)
    print("Executor 已停止")


def start() -> None:
    if healthy():
        print("Executor 已就绪")
        return
    try:
        pid = owned_pid()
    except ProcessIdentityMismatch:
        pid = None
    if pid is not None:
        raise RuntimeError("已有 Executor 进程但健康检查失败，请检查 executor.log")
    settings = get_executor_settings()
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", settings.port)) == 0:
            raise RuntimeError("执行端端口被其他服务占用")
    log_path = ROOT / "executor.log"
    log_path.touch(mode=0o600, exist_ok=True)
    log_path.chmod(0o600)
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-m", "workbench_executor"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
    PID_FILE.write_text(str(child.pid) + "\n", encoding="utf-8")
    PID_FILE.chmod(0o600)
    IDENTITY_FILE.write_text(
        json.dumps({"pid": child.pid, "start_ticks": read_pid_start_ticks(child.pid)}) + "\n",
        encoding="utf-8",
    )
    IDENTITY_FILE.chmod(0o600)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Executor 启动失败，请检查 executor.log")
        if healthy():
            print("Executor 已启动并通过健康检查")
            return
        time.sleep(0.5)
    with contextlib.suppress(ProcessLookupError):
        os.kill(child.pid, signal.SIGTERM)
    raise RuntimeError("Executor 启动超时，请检查 executor.log")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    else:
        if not healthy():
            raise SystemExit("Executor 尚未就绪")
        print("Executor 已就绪")


if __name__ == "__main__":
    main()
