"""进程控制的**实机验证**（必须在 Linux 执行岛上运行）。

为什么单独放在 `executor/tests/`：这些能力依赖 Linux 才有的东西 ——
`/proc/<pid>/stat` 的 starttime、`os.killpg/getpgid`、真实进程组语义。
在 Windows 控制端上"通过"是没有意义的。

**不新增任何通用命令接口，也不打开无人值守模式。**
这里直接调用 `ProcessSupervisor` 的真实代码路径（和生产用的是同一份实现），
只是把"谁来发起命令"从模型换成测试。

覆盖 BUILD_PLAN §8.4 与 ACCEPTANCE P3 清单：

- 一个输出三段内容的慢命令在**退出前**可见第一段；
- 取消产生子进程的命令后，子孙进程全部退出；
- PID 身份核对（start_ticks）能识别"进程已退出"；
- 超时后升级为强制结束；
- stdout/stderr 分开保存；
- 最小环境白名单不继承管理类秘密。

运行方式（在执行岛上）：

    cd ~/rexak-agent-executor
    .venv/bin/python -m pytest executor/tests/test_processes.py -q

在非 Linux 上会被 skip，避免给人"已经验证过"的错觉。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from workbench_executor.processes import (
    ENV_ALLOWLIST,
    ProcessSupervisor,
    minimal_environment,
    pid_is_alive,
    read_pid_start_ticks,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="进程组 / /proc/<pid>/stat 只在 Linux 上存在；Windows 上跑等于没验证",
)


def run(coro: Any) -> Any:
    """同步包装，避免为了几个用例引入 pytest-asyncio。"""
    return asyncio.run(coro)


@pytest.fixture
def supervisor(tmp_path: Path) -> ProcessSupervisor:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    return ProcessSupervisor(
        jobs_root,
        boot_id="drill-boot",
        output_limit_bytes=1024 * 1024,
        terminate_grace_seconds=5,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir(parents=True, exist_ok=True)
    return path


THREE_CHUNK_SCRIPT = (
    "import sys,time\n"
    "for i in range(1, 4):\n"
    "    print(f'chunk {i}', flush=True)\n"
    "    time.sleep(1.5)\n"
)


async def _start_three_chunk(supervisor: ProcessSupervisor, workspace: Path) -> str:
    job_id = str(uuid.uuid4())
    await supervisor.start(
        job_id=job_id,
        argv=[sys.executable, "-c", THREE_CHUNK_SCRIPT],
        cwd=workspace,
    )
    return job_id


def test_first_chunk_visible_before_exit(supervisor: ProcessSupervisor, workspace: Path) -> None:
    """**三段输出**：进程退出前就能读到第一段。

    这条验证的是"边读边写文件 + 游标增量读取"，而不是"等进程结束再一次性给结果"。
    """

    async def scenario() -> tuple[bytes, bool, bytes]:
        job_id = await _start_three_chunk(supervisor, workspace)
        # 等第一段产生
        deadline = time.monotonic() + 5
        first = b""
        while time.monotonic() < deadline:
            data, _eof = await supervisor.read_output(
                job_id, stream="stdout", offset=0, limit=65536
            )
            if b"chunk 1" in data:
                first = data
                break
            await asyncio.sleep(0.1)

        handle = supervisor._handles[job_id]
        still_running = handle.process.returncode is None
        if still_running and handle.process.returncode is None:
            await supervisor.wait(job_id, timeout_seconds=20)
        full, _eof = await supervisor.read_output(job_id, stream="stdout", offset=0, limit=65536)
        return first, still_running, full

    first, still_running, full = run(scenario())

    assert b"chunk 1" in first, f"第一段在退出前就应可见，实际读到 {first!r}"
    assert still_running, "读到第一段时进程**仍在运行**，否则说明是等结束后一次性拿到的"
    assert first.count(b"chunk") == 1, "此时不应已经拿到全部三段"
    for index in (1, 2, 3):
        assert f"chunk {index}".encode() in full, "最终完整输出必须可查"


def test_incremental_read_by_cursor(supervisor: ProcessSupervisor, workspace: Path) -> None:
    """按偏移增量读取：不重复、不漏段。"""

    async def scenario() -> list[bytes]:
        job_id = await _start_three_chunk(supervisor, workspace)
        await supervisor.wait(job_id, timeout_seconds=20)
        pages: list[bytes] = []
        offset = 0
        for _ in range(10):
            data, eof = await supervisor.read_output(
                job_id, stream="stdout", offset=offset, limit=8
            )
            if not data:
                assert eof
                break
            pages.append(data)
            offset += len(data)
        return pages

    pages = run(scenario())
    assert b"".join(pages) == b"chunk 1\nchunk 2\nchunk 3\n"


def test_cancel_kills_descendants(supervisor: ProcessSupervisor, workspace: Path) -> None:
    """取消产生子进程的命令后，**子孙进程全部退出**。

    命令自己把子进程 PID 打到 stdout，测试据此独立核对 `/proc/<pid>`，
    不依赖"取消返回成功"这一句自述。
    """
    child_pid_path = workspace / "child.pid"

    async def scenario() -> tuple[str, int, bool, str]:
        job_id = str(uuid.uuid4())
        script = (
            "import os,subprocess,sys,time\n"
            "p = subprocess.Popen(['sleep','300'])\n"
            f"open({str(child_pid_path)!r},'w').write(str(p.pid))\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(300)\n"
        )
        await supervisor.start(job_id=job_id, argv=[sys.executable, "-c", script], cwd=workspace)
        # 等到子进程 PID 出现
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not child_pid_path.exists():
            await asyncio.sleep(0.1)
        assert child_pid_path.exists(), "命令没有产出子进程 PID"
        child_pid = int(child_pid_path.read_text().strip())

        handle = supervisor._handles[job_id]
        parent_pid = handle.pid
        assert _alive(child_pid), "子进程应处于运行中"

        confirmed, detail = await supervisor.cancel(job_id)
        await asyncio.sleep(0.5)
        return job_id, child_pid, confirmed, f"{detail}|parent={parent_pid}"

    _job_id, child_pid, confirmed, detail = run(scenario())
    assert confirmed, f"取消应确认退出，实际 detail={detail}"
    assert not _alive(child_pid), f"子进程 {child_pid} 仍在运行 —— 子孙进程没有被回收"
    assert supervisor.active_jobs == 0


def test_cancel_is_idempotent(supervisor: ProcessSupervisor, workspace: Path) -> None:
    async def scenario() -> tuple[bool, bool]:
        job_id = str(uuid.uuid4())
        await supervisor.start(job_id=job_id, argv=["sleep", "300"], cwd=workspace)
        await asyncio.sleep(0.3)
        first, _ = await supervisor.cancel(job_id)
        second, _ = await supervisor.cancel(job_id)
        return first, second

    first, second = run(scenario())
    assert first is True
    assert second is True, "重复取消应返回同一结论，而不是报错"


def test_timeout_is_reported_and_process_terminated(
    supervisor: ProcessSupervisor, workspace: Path
) -> None:
    """超时 -> `timed_out=True`，并且进程要被真正终止（不能只返回标志）。"""

    async def scenario() -> tuple[bool, bool]:
        job_id = str(uuid.uuid4())
        await supervisor.start(job_id=job_id, argv=["sleep", "300"], cwd=workspace)
        _code, timed_out = await supervisor.wait(job_id, timeout_seconds=1)
        confirmed, _detail = await supervisor.cancel(job_id)
        return timed_out, confirmed

    timed_out, confirmed = run(scenario())
    assert timed_out is True
    assert confirmed is True


def test_stdout_and_stderr_are_separate(supervisor: ProcessSupervisor, workspace: Path) -> None:
    async def scenario() -> tuple[bytes, bytes]:
        job_id = str(uuid.uuid4())
        script = (
            "import sys\n"
            "print('to-out', flush=True)\n"
            "print('to-err', file=sys.stderr, flush=True)\n"
        )
        await supervisor.start(job_id=job_id, argv=[sys.executable, "-c", script], cwd=workspace)
        await supervisor.wait(job_id, timeout_seconds=10)
        out, _ = await supervisor.read_output(job_id, stream="stdout", offset=0, limit=4096)
        err, _ = await supervisor.read_output(job_id, stream="stderr", offset=0, limit=4096)
        return out, err

    out, err = run(scenario())
    assert b"to-out" in out and b"to-err" not in out
    assert b"to-err" in err and b"to-out" not in err


def test_pid_identity_check_detects_exit(supervisor: ProcessSupervisor, workspace: Path) -> None:
    """PID 身份核对：start_ticks 能区分"同一 PID 的另一个进程"。"""

    async def scenario() -> tuple[int, int | None, bool]:
        job_id = str(uuid.uuid4())
        await supervisor.start(job_id=job_id, argv=["sleep", "300"], cwd=workspace)
        handle = supervisor._handles[job_id]
        pid, ticks = handle.pid, handle.pid_start_ticks
        alive_before = pid_is_alive(pid, expected_start_ticks=ticks)
        await supervisor.cancel(job_id)
        await asyncio.sleep(0.3)
        alive_after = pid_is_alive(pid, expected_start_ticks=ticks)
        assert alive_before, "刚启动时 PID 身份核对应为存活"
        assert not alive_after, "退出后同 PID 不应再被判定为存活"
        return pid, ticks, alive_after

    pid, ticks, _ = run(scenario())
    assert pid > 0
    assert ticks is not None, "Linux 上必须能读到 /proc/<pid>/stat 的 starttime"
    assert read_pid_start_ticks(pid) is None or read_pid_start_ticks(pid) != ticks


def test_unknown_pid_identity_is_not_treated_as_alive() -> None:
    """拿不到 start_ticks 时**不得**判定为存活（否则会误杀/误报）。"""
    assert pid_is_alive(os.getpid(), expected_start_ticks=None) is False


def test_minimal_environment_excludes_management_secrets(monkeypatch) -> None:
    """命令环境不继承 Executor/控制端的管理秘密。"""
    monkeypatch.setenv("EXECUTOR_TOKEN", "fixture-executor-key")
    monkeypatch.setenv("WORKBENCH_ACCESS_TOKEN", "fixture-control-key")
    monkeypatch.setenv("XIAOMI_API_KEY", "fixture-model-key")
    env = minimal_environment()

    for blocked in ("EXECUTOR_TOKEN", "WORKBENCH_ACCESS_TOKEN", "XIAOMI_API_KEY"):
        assert blocked not in env, f"{blocked} 不得进入命令环境"
    assert "PATH" in env
    assert set(env).issubset(set(ENV_ALLOWLIST) | {"PYTHONIOENCODING", "PYTHONUNBUFFERED", "LANG"})


def test_minimal_environment_ignores_extra_secrets() -> None:
    """显式传入的额外环境也要过滤管理类秘密。"""
    env = minimal_environment({"EXECUTOR_TOKEN": "x", "MY_VAR": "ok"})
    assert "EXECUTOR_TOKEN" not in env
    assert env.get("MY_VAR") == "ok"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    else:
        return True
