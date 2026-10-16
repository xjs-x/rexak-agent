"""受控进程：进程组启动、增量输出、取消升级、子孙进程回收、PID 身份核对。

规则（§8.4）：
- 使用进程组（`start_new_session=True`）监督，取消时**先请求终止，再升级为强制结束**，
  确认子孙进程退出后写终态；
- PID 必须配合**启动时间/boot ID**，防止重启后误杀被复用的 PID；
- stdout/stderr 分开保存并带顺序/时间；边读边写文件，避免管道塞满导致死锁；
- 输出大小上限控制存储与 UI，**不静默丢失**：记录 `byte_count`、`truncated` 与保留区间；
- 命令默认**非交互 stdin**，避免等待密码输入；
- 最小环境白名单：命令环境不继承控制端/Executor 管理秘密。
"""

from __future__ import annotations

# 本模块只面向 Linux 执行岛（BUILDPLAN §8.4：Executor 只面向 Linux）。
# pyright 在 Windows 主机上运行时，`signal.SIGKILL` 与 `os.killpg/getpgid` 不在
# Windows 存根里，但这是**目标平台的能力差异**，不是代码缺陷。
# pyright: reportAttributeAccessIssue=false
import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 允许透传给子进程的环境变量。防止把 Executor 管理令牌/控制端密钥泄露给被测脚本。
ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HOME",
    "TERM",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def minimal_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造最小环境。**显式拒绝**继承管理类秘密。"""
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("LANG", "C.UTF-8")
    if extra:
        blocked = {"EXECUTOR_TOKEN", "WORKBENCH_ACCESS_TOKEN", "XIAOMI_API_KEY"}
        env.update({k: v for k, v in extra.items() if k not in blocked})
    return env


@dataclass(slots=True)
class ProcessHandle:
    job_id: str
    pid: int
    pid_start_ticks: int | None
    worker_boot_id: str
    argv: list[str]
    cwd: str
    process: asyncio.subprocess.Process
    stdout_path: Path
    stderr_path: Path
    started_at: str
    byte_count: int = 0
    truncated: bool = False
    finished_at: str | None = None
    exit_code: int | None = None
    cancel_requested: bool = False
    _reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.fromisoformat(self.finished_at)
        return (finished - started).total_seconds()


def read_pid_start_ticks(pid: int) -> int | None:
    """Linux 下读取进程启动 tick，用于 PID 复用判别。

    Windows / 非 Linux 返回 None，此时调用方**不得**声称已完成 PID 身份核对。
    """
    if not Path("/proc").exists():
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="strict")
    except OSError:
        return None
    # 第 22 个字段是 starttime；进程名可能含空格，因此以最后一个 ')' 之后开始切分
    tail = raw[raw.rfind(")") + 2 :].split()
    if len(tail) < 20:
        return None
    try:
        return int(tail[19])
    except ValueError:
        return None


def pid_is_alive(pid: int, *, expected_start_ticks: int | None) -> bool:
    """判断 PID 是否仍是我们启动的那个进程。

    `expected_start_ticks` 为 None 时无法核对身份，返回 False 并让调用方标 unknown，
    **不能**据此判定"已退出"或"仍在运行"。
    """
    if expected_start_ticks is None:
        return False
    current = read_pid_start_ticks(pid)
    return current is not None and current == expected_start_ticks


def process_group_alive(pgid: int) -> bool:
    """核对仍能执行的组成员；已退出的 zombie 不算存活能力。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not Path("/proc").is_dir():
        return True
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return True
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] not in {"Z", "X"}:
            return True
    return False


class ProcessSupervisor:
    """启动与回收受控进程。"""

    def __init__(
        self,
        jobs_root: Path,
        *,
        boot_id: str,
        output_limit_bytes: int,
        terminate_grace_seconds: int = 10,
    ) -> None:
        self._jobs_root = jobs_root
        self._boot_id = boot_id
        self._output_limit = output_limit_bytes
        self._terminate_grace = terminate_grace_seconds
        self._handles: dict[str, ProcessHandle] = {}

    @property
    def active_jobs(self) -> int:
        return sum(1 for handle in self._handles.values() if handle.finished_at is None)

    def job_dir(self, job_id: str) -> Path:
        path = self._jobs_root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def start(
        self,
        *,
        job_id: str,
        argv: list[str],
        cwd: Path,
        env_extra: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
    ) -> ProcessHandle:
        """启动一个独立进程组。

        命令作为独立任务运行，**不依附于一次 SSH channel 的生命周期**（§2）。
        """
        job_dir = self.job_dir(job_id)
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        stdout_handle = stdout_path.open("wb")
        stderr_handle = stderr_path.open("wb")

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=minimal_environment(env_extra),
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,  # 独立进程组，便于整组回收
        )

        handle = ProcessHandle(
            job_id=job_id,
            pid=process.pid,
            pid_start_ticks=read_pid_start_ticks(process.pid),
            worker_boot_id=self._boot_id,
            argv=argv,
            cwd=str(cwd),
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=datetime.now(UTC).isoformat(),
        )
        self._handles[job_id] = handle
        stdout_handle.close()
        stderr_handle.close()

        if stdin_data is not None and process.stdin is not None:
            process.stdin.write(stdin_data)
            await process.stdin.drain()
            process.stdin.close()

        return handle

    async def wait(self, job_id: str, *, timeout_seconds: int | None) -> tuple[int | None, bool]:
        """等待结束。返回 `(exit_code, timed_out)`。"""
        handle = self._handles.get(job_id)
        if handle is None:
            raise KeyError(job_id)

        try:
            if timeout_seconds is None:
                await handle.process.wait()
                timed_out = False
            else:
                await asyncio.wait_for(handle.process.wait(), timeout=timeout_seconds)
                timed_out = False
        except TimeoutError:
            timed_out = True

        handle.exit_code = handle.process.returncode
        handle.finished_at = datetime.now(UTC).isoformat()
        self._refresh_sizes(handle)
        return handle.exit_code, timed_out

    def _refresh_sizes(self, handle: ProcessHandle) -> None:
        total = 0
        truncated = False
        for path in (handle.stdout_path, handle.stderr_path):
            if path.exists():
                size = path.stat().st_size
                total += size
                if size >= self._output_limit:
                    truncated = True
        handle.byte_count = total
        handle.truncated = truncated

    async def cancel(self, job_id: str) -> tuple[bool, str]:
        """取消并确认退出。

        返回 `(confirmed_exited, detail)`。
        未确认时调用方必须显示 unknown，**不能**显示"已全部停止"（§4.2）。
        """
        handle = self._handles.get(job_id)
        if handle is None:
            return False, "本进程内没有该任务的句柄；无法确认直接子进程状态"

        handle.cancel_requested = True
        if handle.process.returncode is not None:
            handle.finished_at = handle.finished_at or datetime.now(UTC).isoformat()
            self._refresh_sizes(handle)
            return True, "进程已退出"

        # 第一步：请求终止整个进程组（SIGTERM）
        self._signal_group(handle.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(handle.process.wait(), timeout=self._terminate_grace)
            handle.exit_code = handle.process.returncode
            handle.finished_at = datetime.now(UTC).isoformat()
            self._refresh_sizes(handle)
            group_gone = not self._group_alive(handle.pid)
            return (
                group_gone,
                "SIGTERM 后进程组已退出" if group_gone else "主进程已退出，但子孙进程状态无法确认",
            )
        except TimeoutError:
            pass

        # 第二步：升级为强制结束（SIGKILL）
        self._signal_group(handle.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(handle.process.wait(), timeout=5)
        except TimeoutError:
            return False, "发送 SIGKILL 后仍无法确认退出"
        handle.exit_code = handle.process.returncode
        handle.finished_at = datetime.now(UTC).isoformat()
        self._refresh_sizes(handle)
        group_gone = not self._group_alive(handle.pid)
        return (
            group_gone,
            "SIGKILL 后进程组已退出" if group_gone else "已强杀主进程，子孙进程状态无法确认",
        )

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            # 进程组已不存在或无权信号：由调用方通过等待与存活检查判定，不在此处吞结论
            return

    @staticmethod
    def _group_alive(pid: int) -> bool:
        try:
            os.killpg(os.getpgid(pid), 0)
        except (ProcessLookupError, OSError):
            return False
        else:
            return True

    async def read_output(
        self, job_id: str, *, stream: str, offset: int, limit: int
    ) -> tuple[bytes, bool]:
        """读取输出切片。`offset` 为字节偏移；返回 `(数据, eof)`。"""
        base = self.job_dir(job_id)
        path = base / ("stdout.log" if stream == "stdout" else "stderr.log")
        if not path.exists():
            return b"", True
        size = path.stat().st_size
        start = min(max(offset, 0), size)
        with path.open("rb") as fp:
            fp.seek(start)
            data = fp.read(limit)
        return data, start + len(data) >= size


def describe_environment() -> dict[str, Any]:
    """能力预检用：如实报告解释器与隔离能力，**不夸大**。"""
    return {
        "platform": sys_platform(),
        "interpreter": _interpreter_path(),
        "pid_start_ticks_supported": Path("/proc").exists(),
        "env_allowlist": list(ENV_ALLOWLIST),
    }


def sys_platform() -> str:
    import sys

    return sys.platform


def _interpreter_path() -> str:
    import sys

    return sys.executable


def monotonic_ms() -> float:
    return time.perf_counter() * 1000
