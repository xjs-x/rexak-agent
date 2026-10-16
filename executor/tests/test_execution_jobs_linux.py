"""job_worker 真进程演练（验收 C01/C02/C08/E03/E04/E06 的执行端部分）。

必须在 Linux 上运行（进程组、`/proc`、信号语义）。在 Windows 上 skip，
避免"跑过了"的错觉 —— 等价覆盖由 `scripts/accept_execution_runtime.py`
在真实 Linux 服务上执行。

运行方式（控制端或执行岛）：

    uv run pytest executor/tests/test_execution_jobs_linux.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from workbench_executor.ledger import Ledger

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="进程组 / forkpty / /proc 只在 Linux 上存在；Windows 上跑等于没验证",
)


def run(coro: Any) -> Any:
    """同步包装，避免为了几个用例引入 pytest-asyncio。"""
    return asyncio.run(coro)


def write_spec(
    data_root: Path,
    job_id: str,
    argv: list[str],
    *,
    execution_timeout_seconds: float | None = None,
    output_limit_bytes: int = 1024 * 1024,
    output_limit_action: str = "preserve_process",
    watch: list[dict[str, Any]] | None = None,
    stdin_text: str | None = None,
) -> Path:
    job_dir = data_root / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    workspace = data_root / "workspaces" / "00000000-0000-0000-0000-000000000001"
    workspace.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": 1,
        "job_id": job_id,
        "operation_id": f"op-{job_id}",
        "session_id": "00000000-0000-0000-0000-000000000001",
        "run_id": "00000000-0000-0000-0000-000000000002",
        "kind": "command",
        "argv": argv,
        "cwd": str(workspace),
        "env": {},
        "stdin_text": stdin_text,
        "execution_timeout_seconds": execution_timeout_seconds,
        "output_limit_bytes": output_limit_bytes,
        "output_limit_action": output_limit_action,
        "watch": watch or [],
        "purpose": "linux drill",
        "created_at": "2026-10-15T00:00:00+00:00",
    }
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2)
    (job_dir / "spec.json").write_text(payload, encoding="utf-8")
    import hashlib

    (job_dir / "spec.sha256").write_text(
        hashlib.sha256(payload.encode()).hexdigest() + "\n", encoding="utf-8"
    )
    async def prepare():
        ledger = Ledger(data_root / "executor-ledger.sqlite3")
        await ledger.open()
        try:
            await ledger.insert(job_id=job_id, operation_id=f"op-{job_id}",
                                request_digest="fixture", kind="command", payload={}, state="received")
        finally:
            await ledger.close()
    run(prepare())
    return job_dir / "spec.json"


def spawn_worker(data_root: Path, job_id: str) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "workbench_executor.job_worker",
            "--job-id",
            job_id,
            "--spec-path",
            str(data_root / "jobs" / job_id / "spec.json"),
            "--data-root",
            str(data_root),
            "--workspace-root",
            str(data_root / "workspaces"),
        ],
        cwd=str(data_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )


def wait_for_completion(job_dir: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = job_dir / "completion.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.1)
    raise AssertionError("等待完成回执超时")


def ledger_state(data_root: Path, job_id: str) -> Any:
    async def read() -> Any:
        ledger = Ledger(data_root / "executor-ledger.sqlite3")
        await ledger.open()
        try:
            return await ledger.get(job_id)
        finally:
            await ledger.close()

    return run(read())


def test_fast_command_completes_with_real_exit_code(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-fast"
    write_spec(data_root, job_id, ["/bin/sh", "-c", "echo hello; echo oops 1>&2; exit 3"])
    process = spawn_worker(data_root, job_id)
    process.wait(timeout=30)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir)
    assert completion["exit_code"] == 3
    assert completion["termination_reason"] == "exited_normally"
    assert completion["group_confirmed_exited"] is True
    assert (job_dir / "stdout.log").read_text(encoding="utf-8") == "hello\n"
    assert (job_dir / "stderr.log").read_text(encoding="utf-8") == "oops\n"
    # stdout/stderr 分开保存；台账投影到终态
    record = ledger_state(data_root, job_id)
    assert record is not None and record.state == "failed" and record.exit_code == 3


def test_output_visible_while_running_then_completion(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-slow"
    write_spec(
        data_root,
        job_id,
        ["/bin/sh", "-c", "echo first; sleep 1.2; echo second; exit 0"],
    )
    process = spawn_worker(data_root, job_id)
    job_dir = data_root / "jobs" / job_id
    deadline = time.monotonic() + 10
    saw_first = False
    while time.monotonic() < deadline:
        try:
            content = (job_dir / "stdout.log").read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        if "first" in content:
            saw_first = True
            break
        time.sleep(0.05)
    assert saw_first, "退出前应能看到第一段输出"
    assert not (job_dir / "completion.json").exists()
    process.wait(timeout=30)
    completion = wait_for_completion(job_dir)
    assert completion["exit_code"] == 0
    assert "second" in (job_dir / "stdout.log").read_text(encoding="utf-8")


def test_execution_timeout_terminates_process_group(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-timeout"
    write_spec(
        data_root,
        job_id,
        ["/bin/sh", "-c", "sleep 30 & wait"],
        execution_timeout_seconds=1,
    )
    process = spawn_worker(data_root, job_id)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir, timeout=20)
    process.wait(timeout=20)
    assert completion["termination_reason"] == "execution_timeout"
    assert completion["group_confirmed_exited"] is True
    record = ledger_state(data_root, job_id)
    assert record is not None and record.state == "failed"


def test_cancel_request_via_control_file(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-cancel"
    write_spec(data_root, job_id, ["/bin/sh", "-c", "sleep 30"])
    process = spawn_worker(data_root, job_id)
    job_dir = data_root / "jobs" / job_id
    # 等 process.json（子进程已启动）后再写取消请求
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (job_dir / "process.json").exists():
        time.sleep(0.05)
    control = job_dir / "control"
    control.mkdir(exist_ok=True)
    (control / "cancel.json").write_text(
        json.dumps({"request_id": "req-1", "escalate": True}), encoding="utf-8"
    )
    completion = wait_for_completion(job_dir, timeout=30)
    process.wait(timeout=30)
    assert completion["termination_reason"] == "user_cancelled"
    assert completion["cancel_request_id"] == "req-1"
    record = ledger_state(data_root, job_id)
    assert record is not None and record.state == "cancelled"


def test_repeated_worker_start_does_not_duplicate_execution(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-claim"
    write_spec(data_root, job_id, ["/bin/sh", "-c", "echo run >> runs.txt; sleep 0.8"])
    first = spawn_worker(data_root, job_id)
    # 立刻启动第二个 worker：必须被 claim 挡住，不能出现第二个实际命令
    second = spawn_worker(data_root, job_id)
    first.wait(timeout=30)
    second.wait(timeout=30)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir)
    assert completion["exit_code"] == 0
    content = (data_root / "workspaces" / "00000000-0000-0000-0000-000000000001" / "runs.txt").read_text(encoding="utf-8")
    assert content.count("run") == 1


def test_output_limit_preserve_process_keeps_running(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-preserve"
    write_spec(
        data_root,
        job_id,
        ["/bin/sh", "-c", "yes 0123456789 | head -c 200000; echo done-marker"],
        output_limit_bytes=1024,
        output_limit_action="preserve_process",
    )
    process = spawn_worker(data_root, job_id)
    process.wait(timeout=60)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir, timeout=30)
    assert completion["termination_reason"] == "exited_normally"
    assert completion["exit_code"] == 0
    assert completion["dropped_bytes"] > 0
    stored = (job_dir / "stdout.log").stat().st_size
    assert stored <= 1024 + 64  # 保留量为额度（允许最后一次写入的边界）
    index = json.loads((job_dir / "output-index.json").read_text(encoding="utf-8"))
    assert index["limit_reached"] is True
    assert index["streams"]["stdout"]["missing_total_bytes"] > 0


def test_output_limit_terminate_stops_the_job(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-terminate"
    write_spec(
        data_root,
        job_id,
        ["/bin/sh", "-c", "seq 1 100000; sleep 30"],
        output_limit_bytes=512,
        output_limit_action="terminate",
    )
    process = spawn_worker(data_root, job_id)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir, timeout=30)
    process.wait(timeout=30)
    assert completion["termination_reason"] == "output_limit_terminate"
    assert completion["group_confirmed_exited"] is True


def test_watch_rule_records_event_and_resource_keeps_running(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-watch"
    write_spec(
        data_root,
        job_id,
        ["/bin/sh", "-c", "echo before; sleep 0.3; echo TOKEN=abc123; sleep 0.3; exit 0"],
        watch=[{"kind": "regex", "pattern": "TOKEN=[0-9a-z]+"}],
    )
    process = spawn_worker(data_root, job_id)
    process.wait(timeout=30)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir)
    assert completion["watch_hits"] >= 1
    assert completion["exit_code"] == 0  # 命中不关闭资源

    async def read_events() -> list[Any]:
        ledger = Ledger(data_root / "executor-ledger.sqlite3")
        await ledger.open()
        try:
            return list(await ledger.read_events(after=0, limit=100))
        finally:
            await ledger.close()

    events = run(read_events())
    types = [row["type"] for row in events]
    assert "resource.watch_match" in types
    assert "resource.exited" in types


def test_completion_receipt_is_immutable_and_hash_matches(tmp_path: Path) -> None:
    data_root = tmp_path / "root"
    job_id = "job-receipt"
    write_spec(data_root, job_id, ["/bin/echo", "receipt"])
    process = spawn_worker(data_root, job_id)
    process.wait(timeout=30)
    job_dir = data_root / "jobs" / job_id
    completion = wait_for_completion(job_dir)
    assert completion["receipt_hash"]
    import hashlib

    hashed = dict(completion)
    hashed.pop("receipt_hash")
    digest = hashlib.sha256(
        json.dumps(hashed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert digest == completion["receipt_hash"]
    record = ledger_state(data_root, job_id)
    assert record is not None and record.completion_path is not None
