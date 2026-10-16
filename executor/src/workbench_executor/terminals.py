"""PTY 终端：生命周期、输入序列化与回执、四模式、屏幕与 resize。

边界（规格 §6.3、R05）：
- 交互 PTY **允许**在 Executor 重启后失去交互能力；保留记录、核对残留进程，
  **不自动重建**、不自动重发输入；
- 单终端输入序号由服务端**持久分配**并绑定 operation_id；同 ID 重试返回原回执，
  不再次写入；不同发送并发时串行；
- 等待状态按**本次输入之后**的输出边界判断：上一次残留提示符不构成完成；
- prompt/quiet 只是观察信号，**不是**退出码；
- 屏幕从 PTY 创建时开始有界维护（pyte），首次读屏不依赖已截断尾部伪造历史；
  重启后从保留日志重建时显式标注 `rebuilt`。

本模块面向 Linux 执行岛：`os.forkpty` / `fcntl.ioctl` / `termios` 仅 POSIX 存在；
Windows 上模块可安全导入（能力报告 pty=unavailable），平台差异不是代码缺陷。
"""

# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import re
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

# fcntl/termios 仅存在于 POSIX；Windows 控制端导入本模块时不得因此崩溃。
# 平台能力差异由 `TerminalRuntime.available` 如实报告（不夸大），不是代码缺陷。
try:  # pragma: no cover - 平台差异
    import fcntl
    import termios
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

import pyte

from workbench_executor._generated.contracts import execution_runtime as proto
from workbench_executor.output_store import (
    CursorError,
    OutputIndex,
    ResourceOutputStore,
    decode_chunk,
    encode_cursor,
    missing_ranges_for,
    parse_cursor,
    read_slice,
    stream_path,
)
from workbench_executor.processes import (
    minimal_environment,
    pid_is_alive,
    process_group_alive,
    read_pid_start_ticks,
)
from workbench_executor.settings import ExecutorSettings
from workbench_executor.watch import WatchMatcher, build_rules

#: 屏幕重建时最多回放的日志字节（有界回放；超出部分标注 truncated）。
MAX_REBUILD_BYTES = 8 * 1024 * 1024
#: 等待轮询粒度。
WAIT_TICK_SECONDS = 0.05
#: 终端进程退出后的 drain 宽限。
DRAIN_GRACE_SECONDS = 2.0
TERMINATE_GRACE_SECONDS = 10.0
KILL_CONFIRM_SECONDS = 5.0
#: 本次输入之后保留用于 prompt 匹配的文本缓冲上限。
TEXT_AFTER_EPOCH_LIMIT = 64 * 1024


class TerminalRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _spawn_pty(
    argv: list[str], cwd: str, env: dict[str, str], rows: int, cols: int
) -> tuple[int, int]:
    """posix_spawn 创建独立会话并重新打开 slave 取得控制终端，避免多线程 fork。"""
    master_fd, slave_fd = os.openpty()
    try:
        slave_path = os.ttyname(slave_fd)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        actions = [(os.POSIX_SPAWN_OPEN, 0, slave_path, os.O_RDWR, 0),
                   (os.POSIX_SPAWN_DUP2, 0, 1), (os.POSIX_SPAWN_DUP2, 0, 2),
                   (os.POSIX_SPAWN_CLOSE, master_fd), (os.POSIX_SPAWN_CLOSE, slave_fd)]
        # chdir 在新解释器内执行；不在多线程服务 fork 后运行 Python。
        bootstrap = "import os,sys;os.chdir(sys.argv[1]);os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
        pid = os.posix_spawn(sys.executable, [sys.executable, "-c", bootstrap, cwd, *argv],
                             env, file_actions=actions, setsid=True)
        os.set_blocking(master_fd, False)
        return pid, master_fd
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)


@dataclass(slots=True)
class TerminalInputResult:
    input_seq: int
    written_bytes: int
    wait_reason: str
    output_page: proto.OutputPageV2 | None
    note: str


@dataclass(slots=True)
class _Session:
    terminal_id: str
    session_id: str
    run_id: str
    operation_id: str
    mode: str
    argv: list[str]
    cwd: str
    rows: int
    cols: int
    pid: int
    pid_start_ticks: int | None
    pgid: int | None
    host_boot_id: str
    master_fd: int
    store: ResourceOutputStore
    matcher: WatchMatcher
    screen: pyte.Screen
    stream: pyte.Stream
    prompt_regex: re.Pattern[str] | None
    quiet_ms: int
    startup_grace_ms: int
    purpose: str
    created_monotonic: float = field(default_factory=time.monotonic)
    last_output_monotonic: float = field(default_factory=time.monotonic)
    total_source_bytes: int = 0
    decode_carry: bytes = b""
    text_after_epoch: str = ""
    epoch_source_bytes: int = 0
    output_event: asyncio.Event = field(default_factory=asyncio.Event)
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    exited: bool = False
    exit_code: int | None = None
    closed: bool = False
    drained: bool = False
    dispatch_notified: bool = False
    read_task: asyncio.Task[None] | None = None
    reap_task: asyncio.Task[None] | None = None
    last_checked_at: str = ""


class TerminalRuntime:
    """PTY 终端的服务侧管理器。只在 Linux 上启用。"""

    def __init__(self, settings: ExecutorSettings, ledger: Any, boot_id: str) -> None:
        self._settings = settings
        self._ledger = ledger
        self._boot_id = boot_id
        self._sessions: dict[str, _Session] = {}
        self._available = os.name == "posix" and hasattr(os, "forkpty")

    @property
    def available(self) -> bool:
        return self._available

    def _require_platform(self) -> None:
        if not self._available:
            raise TerminalRejected(
                "dependency_unavailable", "当前平台不支持 PTY（仅 Linux 执行岛提供该能力）"
            )

    # ------------------------------------------------------------------ #
    # 打开
    # ------------------------------------------------------------------ #

    async def open(
        self, envelope: proto.ExecutionOperationEnvelope, arguments: dict[str, Any]
    ) -> tuple[proto.OperationReceipt, str | None]:
        """创建终端并执行首个等待（按模式）。

        返回 `(receipt, startup_wait_reason)`。
        """
        self._require_platform()
        existing = await self._ledger.get_operation(envelope.operation_id)
        if existing is not None:
            if existing.request_digest != envelope.request_digest:
                raise TerminalRejected(
                    "operation_conflict", "同一 operation_id 复用了不同参数"
                )
            terminal_id = existing.resource_id or ""
            return (
                proto.OperationReceipt(
                    operation_id=envelope.operation_id,
                    action="terminal_open",
                    status="accepted",
                    terminal_id=terminal_id,
                    resource_id=terminal_id,
                    deduplicated=True,
                    accepted_at=existing.accepted_at,
                    note="幂等重放：返回原终端",
                ),
                None,
            )

        session_id = envelope.session_id
        active_total, active_pty = await self._ledger.count_active_for_session(session_id)
        if active_pty >= self._settings.session_max_pty:
            raise TerminalRejected(
                "resource_limit",
                f"该会话 PTY 数量已达上限（{self._settings.session_max_pty}）",
            )
        if active_total >= self._settings.session_max_resources:
            raise TerminalRejected(
                "resource_limit",
                f"该会话并行资源已达上限（{self._settings.session_max_resources}）",
            )
        global_active = await self._ledger.count_active_global()
        if global_active >= self._settings.global_max_resources:
            raise TerminalRejected("resource_limit", "执行端全局资源已达上限")

        argv, cwd = self._resolve_argv(envelope, arguments)
        rows = int(arguments.get("rows") or 24)
        cols = int(arguments.get("cols") or 80)
        self._validate_grid(rows, cols)
        prompt_regex = self._compile_prompt(arguments.get("prompt_regex"))
        quiet_ms = int(arguments.get("quiet_ms") or self._settings.pty_quiet_ms)
        startup_grace_ms = int(
            arguments.get("startup_grace_ms") or self._settings.pty_startup_grace_ms
        )
        watch_rules = build_rules(list(arguments.get("watch") or []))
        mode = str(arguments.get("mode") or "interactive")
        if mode not in {item.value for item in proto.TerminalMode}:
            raise TerminalRejected("validation_failed", f"未知终端模式：{mode}")

        terminal_id = str(uuid4())
        remaining = self._settings.session_log_budget_bytes - await self._ledger.session_budget_used(session_id)
        reservation = min(self._settings.resource_log_limit_bytes, remaining)
        if reservation < self._settings.min_output_reservation_bytes:
            raise TerminalRejected("resource_limit", "会话日志预算不足，已有资源保留")
        try:
            await self._ledger.reserve_output_budget(
                resource_id=terminal_id, session_id=session_id, bytes_needed=reservation,
                session_budget=self._settings.session_log_budget_bytes,
            )
        except ValueError as exc:
            raise TerminalRejected("resource_limit", str(exc)) from exc
        log_dir = self._settings.terminal_log_root / terminal_id
        log_dir.mkdir(parents=True, exist_ok=True)
        store = ResourceOutputStore(
            log_dir,
            resource_id=terminal_id,
            limit_bytes=reservation,
            limit_action=str(arguments.get("output_limit_action") or "preserve_process"),
            streams=("stdout",),
        )
        store.open()

        env = minimal_environment(
            {str(k): str(v) for k, v in (arguments.get("env") or {}).items()}
        )
        env.setdefault("TERM", "xterm-256color")
        # 接收事实必须早于 fork；启动窗口崩溃也不能自动再次创建同一程序。
        await self._ledger.insert_operation(
            operation_id=envelope.operation_id, session_id=session_id, run_id=envelope.run_id,
            tool_call_id=envelope.tool_call_id, request_digest=envelope.request_digest,
            action="terminal_open", resource_id=terminal_id,
            owner_generation=envelope.owner_generation,
            dispatch_control_version=envelope.dispatch_control_version,
            session_control_seq=envelope.session_control_seq,
            delegation_id=envelope.delegation_id, delegation_revision=envelope.delegation_revision,
            receipt={"terminal_id": terminal_id, "state": "starting"},
        )
        await self._ledger.insert_terminal(
            terminal_id=terminal_id, session_id=session_id, run_id=envelope.run_id,
            operation_id=envelope.operation_id, mode=mode,
            argv_json=json.dumps(argv, ensure_ascii=False), cwd=str(cwd), state="starting",
            service_boot_id=self._boot_id, rows=rows, cols=cols,
            prompt_regex=arguments.get("prompt_regex"), quiet_ms=quiet_ms,
            startup_grace_ms=startup_grace_ms,
            watch_json=json.dumps(arguments.get("watch") or [], ensure_ascii=False),
            log_dir=str(log_dir), purpose=str(arguments.get("description") or ""),
            output_limit_action=str(store.index.limit_action),
        )
        try:
            pid, master_fd = await asyncio.to_thread(_spawn_pty, argv, str(cwd), env, rows, cols)
        except OSError as exc:
            store.close()
            await self._ledger.release_output_budget(terminal_id)
            await self._ledger.update_terminal(terminal_id, state="closed", interaction_state="lost")
            await self._ledger.complete_operation(envelope.operation_id, status="failed")
            raise TerminalRejected("execution_failed", f"PTY 启动失败：{exc}") from exc

        screen = pyte.Screen(cols, rows)
        stream = pyte.Stream(screen)
        session = _Session(
            terminal_id=terminal_id,
            session_id=session_id,
            run_id=envelope.run_id,
            operation_id=envelope.operation_id,
            mode=mode,
            argv=argv,
            cwd=str(cwd),
            rows=rows,
            cols=cols,
            pid=pid,
            pid_start_ticks=read_pid_start_ticks(pid),
            pgid=self._safe_pgid(pid),
            host_boot_id=self._host_boot_id(),
            master_fd=master_fd,
            store=store,
            matcher=WatchMatcher(watch_rules),
            screen=screen,
            stream=stream,
            prompt_regex=prompt_regex,
            quiet_ms=quiet_ms,
            startup_grace_ms=startup_grace_ms,
            purpose=str(arguments.get("description") or ""),
        )
        self._sessions[terminal_id] = session
        await self._ledger.update_terminal(
            terminal_id=terminal_id,
            state="running",
            pid=pid,
            pid_start_ticks=session.pid_start_ticks,
            pgid=session.pgid,
            host_boot_id=session.host_boot_id,
            service_boot_id=self._boot_id,
        )
        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_readable, session)
        session.read_task = asyncio.create_task(self._reap_loop(session))
        await self._emit(
            proto.ExecutionEventType.RESOURCE_STARTED.value,
            session,
            {"mode": mode, "pid": pid, "rows": rows, "cols": cols},
        )

        wait_reason: str | None = None
        if mode == proto.TerminalMode.INTERACTIVE.value:
            wait_reason = await self._wait(
                session,
                wait_ms=startup_grace_ms,
                prompt=prompt_regex,
                quiet_ms=quiet_ms,
                allow_exited=True,
            )
        elif mode == proto.TerminalMode.DISPATCH.value:
            task = asyncio.create_task(self._dispatch_notify(session))
            task.add_done_callback(lambda item: item.exception())
        # hands_free / monitor：创建后立即返回。

        receipt = proto.OperationReceipt(
            operation_id=envelope.operation_id,
            action="terminal_open",
            status="accepted",
            terminal_id=terminal_id,
            resource_id=terminal_id,
            process_state=proto.ResourceStatus.RUNNING,
            interaction_state=proto.InteractionState.AVAILABLE,
            wait_reason=(proto.WaitReason(wait_reason) if wait_reason else None),
            resource=await self.status(terminal_id),
            accepted_at=_utcnow(),
            note=(
                "终端已创建；进程可继续运行，交互不会因一次读取而停止。"
                "prompt/quiet 只表示观察到的输出边界，不代表进程成功退出。"
            ),
        )
        return receipt, wait_reason

    def _resolve_argv(
        self, envelope: proto.ExecutionOperationEnvelope, arguments: dict[str, Any]
    ) -> tuple[list[str], Path]:
        workspace_id = str(arguments.get("workspace_id") or envelope.session_id)
        from workbench_executor.filesystem import workspace_for

        workspace_root = workspace_for(self._settings.workspace_root, workspace_id).root
        workspace_root.mkdir(parents=True, exist_ok=True)
        cwd_raw = str(arguments.get("cwd") or ".")
        cwd = (workspace_root / cwd_raw).resolve() if cwd_raw not in {"", "."} else workspace_root
        if cwd != workspace_root and workspace_root not in cwd.parents:
            raise TerminalRejected("validation_failed", f"工作目录越界：{cwd_raw}")
        if arguments.get("argv"):
            argv = [str(item) for item in arguments["argv"]]
        else:
            command = str(arguments.get("command") or "")
            if not command:
                raise TerminalRejected("validation_failed", "command 与 argv 必须二选一")
            argv = [self._settings.shell_path, "-c", command]
        return argv, cwd

    def _validate_grid(self, rows: int, cols: int) -> None:
        if not (
            proto.TERMINAL_MIN_SIDE <= rows <= proto.TERMINAL_MAX_SIDE
            and proto.TERMINAL_MIN_SIDE <= cols <= proto.TERMINAL_MAX_SIDE
        ):
            raise TerminalRejected(
                "validation_failed",
                f"终端尺寸必须在 {proto.TERMINAL_MIN_SIDE}–{proto.TERMINAL_MAX_SIDE} 之间",
            )
        if rows * cols > proto.TERMINAL_MAX_GRID:
            raise TerminalRejected("validation_failed", "终端网格过大")

    def _compile_prompt(self, pattern: Any) -> re.Pattern[str] | None:
        if not pattern:
            return None
        text = str(pattern)
        if len(text) > proto.MAX_WATCH_PATTERN_CHARS:
            raise TerminalRejected("validation_failed", "prompt_regex 过长")
        try:
            return re.compile(text)
        except re.error as exc:
            raise TerminalRejected("validation_failed", f"prompt_regex 非法：{exc}") from exc

    def _host_boot_id(self) -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown-boot"

    @staticmethod
    def _safe_pgid(pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    # 读循环
    # ------------------------------------------------------------------ #

    def _on_readable(self, session: _Session) -> None:
        try:
            data = os.read(session.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno == errno.EIO:
                # Linux：PTY 从端关闭后读主端返回 EIO，等价于 EOF。
                self._mark_drained(session)
                return
            self._mark_drained(session)
            return
        if not data:
            self._mark_drained(session)
            return
        self._consume(session, data)

    def _consume(self, session: _Session, data: bytes) -> None:
        session.total_source_bytes += len(data)
        session.last_output_monotonic = time.monotonic()
        # 显式 terminate 额度耗尽：记录后交由回收路径处理（终端默认保活）。
        with contextlib.suppress(Exception):
            session.store.write("stdout", data)
        combined = session.decode_carry + data
        decoded = decode_chunk(combined)
        session.decode_carry = combined[decoded.consumed :]
        if decoded.text:
            if len(session.text_after_epoch) < TEXT_AFTER_EPOCH_LIMIT:
                session.text_after_epoch += decoded.text
                if len(session.text_after_epoch) > TEXT_AFTER_EPOCH_LIMIT:
                    session.text_after_epoch = session.text_after_epoch[-TEXT_AFTER_EPOCH_LIMIT:]
            with contextlib.suppress(Exception):
                session.stream.feed(decoded.text)
        update = session.matcher.feed(decoded.text)
        for match in update.matches:
            asyncio.get_running_loop().create_task(
                self._emit(
                    proto.ExecutionEventType.WATCH_MATCH.value,
                    session,
                    match.to_payload(),
                )
            )
        session.output_event.set()
        session.output_event.clear()

    def _mark_drained(self, session: _Session) -> None:
        if session.drained:
            return
        session.drained = True
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(session.master_fd)
        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        session.output_event.set()

    async def _reap_loop(self, session: _Session) -> None:
        """等待子进程退出并落库。主进程退出不代表子孙全部退出，单独核对进程组。"""
        last_index_flush = time.monotonic()
        index_warning = False
        while not session.exited:
            if time.monotonic() - last_index_flush >= 1.0:
                try:
                    session.store.flush_index()
                except OSError as exc:
                    if not index_warning:
                        await self._emit(proto.ExecutionEventType.RESOURCE_WARNING.value, session,
                                         {"reason": "output_index_write_failed", "error": str(exc)})
                        index_warning = True
                last_index_flush = time.monotonic()
            try:
                waited, status = os.waitpid(session.pid, os.WNOHANG)
            except ChildProcessError:
                waited = session.pid
                status = 0
            if waited == session.pid:
                session.exit_code = (
                    os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
                )
                session.exited = True
                break
            await asyncio.sleep(0.2)
        # drain 宽限：让读循环把残余输出收干净
        deadline = time.monotonic() + DRAIN_GRACE_SECONDS
        while not session.drained and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        session.store.close()
        await self._ledger.settle_output_budget(session.terminal_id, session.store.stored_total)
        group_confirmed = not self._group_alive(session.pid)
        await self._ledger.update_terminal(
            session.terminal_id,
            state="closed" if session.closed and group_confirmed else "exited" if group_confirmed else "unknown",
            interaction_state="not_applicable",
            exited_at=_utcnow(),
            exit_code=session.exit_code,
            termination_reason="exited_normally" if group_confirmed else "unknown",
        )
        await self._emit(
            proto.ExecutionEventType.RESOURCE_EXITED.value,
            session,
            {
                "exit_code": session.exit_code,
                "group_confirmed_exited": group_confirmed,
                "note": "" if group_confirmed else "主进程已退出；子孙进程状态无法确认",
            },
        )

    @staticmethod
    def _group_alive(pid: int) -> bool:
        return process_group_alive(pid)

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_input_bytes(arguments: proto.TerminalSendArguments) -> bytes:
        """按固定顺序构造输入字节：text -> submit -> keys -> hex -> paste。

        任何非法参数在**写入之前**整体拒绝；不会发送一半才校验。
        """
        pieces: list[bytes] = []
        if arguments.text is not None:
            pieces.append(arguments.text.encode("utf-8"))
        if arguments.submit:
            pieces.append(proto.KEY_BYTES["Enter"])
        for key in arguments.keys:
            if key not in proto.KEY_BYTES:
                raise TerminalRejected(
                    "validation_failed",
                    f"未知按键：{key}；支持：{', '.join(proto.SUPPORTED_KEYS)}",
                )
            pieces.append(proto.KEY_BYTES[key])
        if len(arguments.hex) > proto.MAX_HEX_ITEMS:
            raise TerminalRejected("validation_failed", "hex 输入项过多")
        hex_total = 0
        for item in arguments.hex:
            try:
                raw = bytes.fromhex(item)
            except ValueError as exc:
                raise TerminalRejected("validation_failed", f"非法 hex 字节：{item!r}") from exc
            hex_total += len(raw)
            if hex_total > proto.MAX_HEX_BYTES:
                raise TerminalRejected("validation_failed", "hex 输入总长度超限")
            pieces.append(raw)
        if arguments.paste is not None:
            pieces.append(proto.BRACKETED_PASTE_START)
            pieces.append(arguments.paste.encode("utf-8"))
            pieces.append(proto.BRACKETED_PASTE_END)
        return b"".join(pieces)

    async def send(
        self,
        envelope: proto.ExecutionOperationEnvelope,
        arguments: proto.TerminalSendArguments,
    ) -> proto.OperationReceipt:
        self._require_platform()
        session = self._sessions.get(arguments.terminal_id)
        if session is None:
            existing = await self._ledger.get_operation(envelope.operation_id)
            if existing is not None and existing.request_digest == envelope.request_digest:
                receipt = existing.receipt
                return proto.OperationReceipt(
                    operation_id=envelope.operation_id,
                    action="terminal_send",
                    status="succeeded" if receipt.get("status") != "writing" else "unknown",
                    terminal_id=arguments.terminal_id,
                    resource_id=arguments.terminal_id,
                    deduplicated=True,
                    accepted_at=existing.accepted_at,
                    wait_reason=(
                        proto.WaitReason(receipt["wait_reason"])
                        if receipt.get("wait_reason")
                        else None
                    ),
                    note="相同 operation_id 的重试：返回原回执，未再次写入。",
                )
            record = await self._ledger.get_terminal(arguments.terminal_id)
            if record is None:
                raise TerminalRejected("resource_not_found", "终端不存在")
            raise TerminalRejected(
                "interaction_lost",
                "该终端当前不可交互（服务重启后交互丢失，或终端已退出/关闭）；"
                "历史日志仍可读取，但不会自动重建终端或重发输入。",
            )
        if session.session_id != envelope.session_id:
            raise TerminalRejected("resource_owner_mismatch", "终端不属于当前会话")
        if session.closed:
            raise TerminalRejected("resource_not_found", "终端已关闭")
        if session.exited:
            raise TerminalRejected(
                "interaction_lost", "终端进程已退出，不能再发送输入；历史日志仍可读取"
            )

        data = self.build_input_bytes(arguments)
        digest_extra = hashlib.sha256(data).hexdigest()
        if not envelope.request_digest.endswith(digest_extra[:8]):
            # 控制端摘要不包含原始字节时的独立校验：此处仅记录，不改变幂等语义。
            pass

        async with session.input_lock:
            seq, _receipt, deduplicated = await self._ledger.allocate_input_seq(
                terminal_id=session.terminal_id,
                operation_id=envelope.operation_id,
                request_digest=envelope.request_digest,
                receipt={"status": "writing", "input_seq": 0},
            )
            if deduplicated:
                stored = await self._ledger.get_input_receipt(envelope.operation_id)
                original = stored[1] if stored else {}
                return proto.OperationReceipt(
                    operation_id=envelope.operation_id,
                    action="terminal_send",
                    status="succeeded" if original.get("status") != "writing" else "unknown",
                    terminal_id=session.terminal_id,
                    resource_id=session.terminal_id,
                    deduplicated=True,
                    wait_reason=(
                        proto.WaitReason(original["wait_reason"])
                        if original.get("wait_reason")
                        else None
                    ),
                    note="相同 operation_id 的重试：返回原回执，未再次写入。",
                )
            # 序号持久化后才真正写入字节；写入中途崩溃 -> 回执保持 writing，
            # 恢复路径不会自动重发（未知窗口明确）。
            await self._ledger.update_terminal(session.terminal_id, input_seq=seq)
            session.epoch_source_bytes = session.total_source_bytes
            session.text_after_epoch = ""
            fence = await self._ledger.get_fence(envelope.session_id)
            if fence is not None:
                revoked = {str(item.get("delegation_id")): int(item.get("revision", 0))
                           for item in fence.get("revocations") or []}
                if (fence["run_id"] != envelope.run_id
                    or int(fence["owner_generation"]) != envelope.owner_generation
                    or int(fence["control_version"]) != envelope.dispatch_control_version
                    or (envelope.delegation_id in revoked and int(envelope.delegation_revision or 0)
                        <= revoked[envelope.delegation_id])):
                    raise TerminalRejected("stale_control", "排队输入所属控制版本或委托已失效，未写入字节")
            written = await self._write_all(session, data)
            wait_reason = "wait_timeout"
            output_page: proto.OutputPageV2 | None = None
            if arguments.wait:
                prompt = (
                    self._compile_prompt(arguments.prompt_regex)
                    if arguments.prompt_regex
                    else session.prompt_regex
                )
                reason = await self._wait(
                    session,
                    wait_ms=int(arguments.wait_ms),
                    prompt=prompt,
                    quiet_ms=(
                        int(arguments.quiet_ms) if arguments.quiet_ms is not None else session.quiet_ms
                    ),
                    allow_exited=True,
                )
                wait_reason = reason or "wait_timeout"
                output_page = await self.read_page(
                    session.terminal_id, cursor=None, stream="stdout", limit=16384, view="stream"
                )
            receipt_payload = {
                "status": "written",
                "input_seq": seq,
                "written_bytes": written,
                "wait_reason": wait_reason,
                "written_at": _utcnow(),
                "note": "输入已按 text->submit->keys->hex->paste 顺序写入；prompt/quiet 不是退出码。",
            }
            await self._ledger.mark_input_receipt(envelope.operation_id, receipt_payload)
            await self._ledger.complete_operation(
                envelope.operation_id, status="executed", resource_id=session.terminal_id
            )
        return proto.OperationReceipt(
            operation_id=envelope.operation_id,
            action="terminal_send",
            status="succeeded",
            terminal_id=session.terminal_id,
            resource_id=session.terminal_id,
            process_state=(
                proto.ResourceStatus.EXITED if session.exited else proto.ResourceStatus.RUNNING
            ),
            interaction_state=(
                proto.InteractionState.LOST
                if session.exited
                else proto.InteractionState.AVAILABLE
            ),
            wait_reason=proto.WaitReason(wait_reason),
            exit_code=session.exit_code,
            output=output_page,
            accepted_at=_utcnow(),
            note="本次发送操作成功；这不表示被控制的程序执行成功或已退出。",
        )

    async def _write_all(self, session: _Session, data: bytes) -> int:
        """写入 master fd（非阻塞 + 重试），返回写入字节数。"""
        if not data:
            return 0
        written = 0
        deadline = time.monotonic() + 5.0
        fd = session.master_fd
        while written < len(data):
            try:
                written += os.write(fd, data[written:])
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TerminalRejected("resource_limit", "PTY 写缓冲持续忙，输入未完成") from None
                await asyncio.sleep(0.005)
            except OSError as exc:
                raise TerminalRejected(
                    "interaction_lost", f"PTY 写入失败：{exc}（终端可能已退出）"
                ) from exc
        return written

    # ------------------------------------------------------------------ #
    # 等待
    # ------------------------------------------------------------------ #

    async def _wait(
        self,
        session: _Session,
        *,
        wait_ms: int,
        prompt: re.Pattern[str] | None,
        quiet_ms: int,
        allow_exited: bool,
    ) -> str:
        """等待本次输入之后的输出边界。

        返回与 `wait_reason` 对应的字符串：output / prompt / quiet / exited /
        wait_timeout / watch_match。
        """
        deadline = time.monotonic() + max(0, wait_ms) / 1000.0
        quiet_deadline: float | None = None
        saw_output = session.total_source_bytes > session.epoch_source_bytes
        while True:
            now = time.monotonic()
            if (
                prompt is not None
                and session.text_after_epoch
                and prompt.search(session.text_after_epoch)
            ):
                return "prompt"
            if session.exited and allow_exited:
                return "exited"
            if saw_output and quiet_ms > 0:
                idle_ms = (now - session.last_output_monotonic) * 1000.0
                if idle_ms >= quiet_ms:
                    return "quiet"
                quiet_deadline = session.last_output_monotonic + quiet_ms / 1000.0
            if now >= deadline:
                return "output" if saw_output else "wait_timeout"
            wait_until = deadline
            if quiet_deadline is not None:
                wait_until = min(wait_until, quiet_deadline)
            timeout = max(0.0, min(WAIT_TICK_SECONDS, wait_until - now))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.output_event.wait(), timeout=timeout)
            if session.total_source_bytes > session.epoch_source_bytes:
                saw_output = True

    async def _dispatch_notify(self, session: _Session) -> None:
        """dispatch 模式：首个等待条件满足时记录一次通知。通知不是"进程已成功退出"。"""
        if session.dispatch_notified:
            return
        session.dispatch_notified = True
        reason = await self._wait(
            session,
            wait_ms=session.startup_grace_ms,
            prompt=session.prompt_regex,
            quiet_ms=session.quiet_ms,
            allow_exited=True,
        )
        await self._emit(
            proto.ExecutionEventType.DISPATCH_NOTIFIED.value,
            session,
            {
                "wait_reason": reason,
                "note": "首次等待条件已满足；该通知不代表进程已成功退出。",
            },
        )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    async def read_page(
        self,
        terminal_id: str,
        *,
        cursor: str | None,
        stream: str,
        limit: int,
        view: str = "stream",
        max_lines: int | None = None,
    ) -> proto.OutputPageV2:
        session = self._sessions.get(terminal_id)
        record = await self._ledger.get_terminal(terminal_id)
        if session is None and record is None:
            raise TerminalRejected("resource_not_found", f"终端不存在：{terminal_id}")
        if view == "screen":
            return await self._screen_page(terminal_id, session, record)

        if session is not None:
            log_dir = Path(str(session.store.path_for("stdout").parent))
        else:
            log_dir = Path(str(record["log_dir"]))
        try:
            resolved_stream, offset = parse_cursor(
                cursor,
                resource_id=terminal_id,
                # 终端为合流：默认 stream 即调用方请求的 stream（只允许 stdout）。
                default_stream=stream or "stdout",
            )
        except CursorError as exc:
            raise TerminalRejected("invalid_cursor", str(exc)) from exc
        if resolved_stream != "stdout":
            raise TerminalRejected("validation_failed", "PTY 为合流，只能读取 stdout")
        path = stream_path(log_dir, "stdout")
        data, size = read_slice(path, offset=offset, limit=limit)
        if max_lines is not None:
            data = b"".join(data.splitlines(keepends=True)[:max(1, max_lines)])
        decoded = decode_chunk(data)
        if decoded.consumed < len(data) and offset + len(data) < size:
            extra, size = read_slice(path, offset=offset + len(data), limit=4)
            decoded = decode_chunk(data + extra)
        next_offset = offset + decoded.consumed
        index = session.store.index if session is not None else OutputIndex.load(log_dir / "output-index.json", resource_id=terminal_id)
        missing = missing_ranges_for(
            index, "stdout", stored_start=0,
            stored_end=index.streams["stdout"].stored_bytes if index else next_offset,
        )
        entry = index.streams.get("stdout") if index is not None else None
        exit_info = session.exited if session is not None else record["state"] in {
            "exited",
            "closed",
        }
        return proto.OutputPageV2(
            resource_id=terminal_id,
            stream="stdout",
            cursor=cursor,
            next_cursor=encode_cursor(terminal_id, "stdout", next_offset),
            byte_start=offset + (entry.dropped_before(offset) if entry else 0),
            byte_end=next_offset + (entry.dropped_before(next_offset) if entry else 0),
            text=decoded.text,
            byte_count=decoded.consumed,
            at_current_end=next_offset >= size,
            process_exited=bool(exit_info),
            truncated=bool(index.limit_reached) if index else False,
            missing_ranges=[
                proto.ByteRange(start=start, end=end, reason=reason)
                for start, end, reason in missing
            ],
            encoding_error=decoded.error_detail or None,
            note="PTY 输出为合流（stdout/stderr 不可分离）。",
        )

    async def _screen_page(
        self, terminal_id: str, session: _Session | None, record: Any
    ) -> proto.OutputPageV2:
        """屏幕视图。实时会话用维护中的 pyte 屏幕；重启后从保留日志重建。"""
        if session is not None:
            view = proto.TerminalScreenView(
                rows=session.rows,
                cols=session.cols,
                lines=list(session.screen.display),
                cursor_x=session.screen.cursor.x,
                cursor_y=session.screen.cursor.y,
                captured_at=_utcnow(),
                rebuilt=False,
                truncated=False,
                note="实时屏幕；从 PTY 创建时开始维护。",
            )
            return proto.OutputPageV2(
                resource_id=terminal_id,
                stream="screen",
                text="\n".join(view.lines),
                at_current_end=True,
                process_exited=session.exited,
                truncated=False,
            )
        # 重启后：交互丢失，屏幕从保留日志有界重建（明确标注 rebuilt）
        rebuilt = self._rebuild_screen(terminal_id, record)
        return proto.OutputPageV2(
            resource_id=terminal_id,
            stream="screen",
            text="\n".join(rebuilt.lines),
            at_current_end=True,
            process_exited=record["state"] in {"exited", "closed"},
            truncated=rebuilt.truncated,
            note=rebuilt.note,
        )

    def _rebuild_screen(self, terminal_id: str, record: Any) -> proto.TerminalScreenView:
        rows = int(record["rows"])
        cols = int(record["cols"])
        screen = pyte.Screen(cols, rows)
        stream = pyte.Stream(screen)
        log_dir = Path(str(record["log_dir"]))
        truncated = False
        try:
            size = (log_dir / "stdout.log").stat().st_size
            if size > MAX_REBUILD_BYTES:
                truncated = True
            data, _ = read_slice(log_dir / "stdout.log", offset=max(0, size - MAX_REBUILD_BYTES), limit=MAX_REBUILD_BYTES)
        except OSError:
            data = b""
        if data:
            decoded = decode_chunk(data)
            with contextlib.suppress(Exception):
                stream.feed(decoded.text)
        index = OutputIndex.load(log_dir / "output-index.json", resource_id=terminal_id)
        if index is not None and any(entry.drops for entry in index.streams.values()):
            truncated = True
        return proto.TerminalScreenView(
            rows=rows,
            cols=cols,
            lines=list(screen.display),
            cursor_x=screen.cursor.x,
            cursor_y=screen.cursor.y,
            captured_at=_utcnow(),
            rebuilt=True,
            truncated=truncated,
            note=(
                "该屏幕由保留日志重组（服务重启后实时屏幕状态不可恢复）；"
                "如果日志因额度被截断，屏幕可能不完整。"
            ),
        )

    # ------------------------------------------------------------------ #
    # resize / list / close
    # ------------------------------------------------------------------ #

    async def resize(
        self,
        envelope: proto.ExecutionOperationEnvelope,
        arguments: proto.TerminalResizeArguments,
    ) -> proto.OperationReceipt:
        self._require_platform()
        session = self._sessions.get(arguments.terminal_id)
        if session is None:
            raise TerminalRejected("interaction_lost", "终端当前不可交互，不能调整尺寸")
        if session.exited or session.closed:
            raise TerminalRejected("interaction_lost", "终端已退出或关闭")
        if session.session_id != envelope.session_id:
            raise TerminalRejected("resource_owner_mismatch", "终端不属于当前会话")
        self._validate_grid(arguments.rows, arguments.cols)
        # 原子调整 PTY(ioctl) 与终端模拟器(pyte)
        winsize = struct.pack("HHHH", arguments.rows, arguments.cols, 0, 0)
        try:
            fcntl.ioctl(session.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError as exc:
            raise TerminalRejected("interaction_lost", f"resize 失败：{exc}") from exc
        session.screen.resize(arguments.rows, arguments.cols)
        session.rows, session.cols = arguments.rows, arguments.cols
        await self._ledger.update_terminal(
            session.terminal_id, rows=arguments.rows, cols=arguments.cols
        )
        await self._emit(
            proto.ExecutionEventType.RESOURCE_WARNING.value,
            session,
            {"scope": "resize", "rows": arguments.rows, "cols": arguments.cols},
        )
        return proto.OperationReceipt(
            operation_id=envelope.operation_id,
            action="terminal_resize",
            status="succeeded",
            terminal_id=session.terminal_id,
            resource_id=session.terminal_id,
            process_state=proto.ResourceStatus.RUNNING,
            interaction_state=proto.InteractionState.AVAILABLE,
            accepted_at=_utcnow(),
            note="PTY 与终端模拟器已同步调整并记录。",
        )

    async def list_terminals(
        self, session_id: str, *, limit: int, offset: int
    ) -> tuple[list[proto.ResourceStatusView], int]:
        rows, total = await self._ledger.list_terminals(session_id, limit=limit, offset=offset)
        views: list[proto.ResourceStatusView] = []
        for row in rows:
            views.append(self._status_from_row(row))
        return views, total

    def _status_from_row(self, row: Any) -> proto.ResourceStatusView:
        terminal_id = str(row["terminal_id"])
        live = self._sessions.get(terminal_id)
        state = str(row["state"])
        interaction = str(row["interaction_state"])
        if live is None and state in {"starting", "running"} and interaction == "available":
            # 服务重启后：交互事实上已经丢失（记录在核对时修正，这里兜底显示）
            interaction = "lost"
        return proto.ResourceStatusView(
            resource_id=terminal_id,
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
            kind=proto.ResourceKind.TERMINAL,
            state=proto.ResourceStatus(state) if state in {item.value for item in proto.ResourceStatus} else proto.ResourceStatus.UNKNOWN,
            interaction_state=(
                proto.InteractionState(interaction)
                if interaction in {item.value for item in proto.InteractionState}
                else proto.InteractionState.LOST
            ),
            mode=str(row["mode"]),
            purpose=str(row["purpose"]),
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            exited_at=row["exited_at"],
            closed_at=row["closed_at"],
            exit_code=row["exit_code"],
            termination_reason=(
                proto.TerminationReason(row["termination_reason"])
                if row["termination_reason"]
                in {item.value for item in proto.TerminationReason}
                else None
            ),
            output_limit_action=proto.OutputLimitAction(str(row["output_limit_action"])),
            cancel_requested=False,
            cancel_confirmed=bool(row["closed_at"]),
            last_checked_at=_utcnow(),
            notes=[
                "交互已丢失（服务重启）：历史记录与日志保留，不自动重建。"
                if interaction == "lost"
                else ""
            ]
            if interaction == "lost"
            else [],
        )

    async def status(self, terminal_id: str) -> proto.ResourceStatusView | None:
        row = await self._ledger.get_terminal(terminal_id)
        if row is None:
            return None
        return self._status_from_row(row)

    async def close(
        self,
        envelope: proto.ExecutionOperationEnvelope,
        arguments: proto.TerminalCloseArguments,
        *,
        escalate: bool = True,
        expected_session: str | None = None,
    ) -> proto.OperationReceipt:
        self._require_platform()
        terminal_id = arguments.terminal_id
        record = await self._ledger.get_terminal(terminal_id)
        if record is None:
            raise TerminalRejected("resource_not_found", f"终端不存在：{terminal_id}")
        if str(record["session_id"]) != (expected_session or envelope.session_id):
            raise TerminalRejected("resource_owner_mismatch", "终端不属于当前会话")
        if str(record["state"]) == "closed":
            return proto.OperationReceipt(
                operation_id=envelope.operation_id,
                action="terminal_close",
                status="succeeded",
                terminal_id=terminal_id,
                resource_id=terminal_id,
                process_state=proto.ResourceStatus.CLOSED,
                process_confirmed_exited=True,
                interaction_state=proto.InteractionState.NOT_APPLICABLE,
                accepted_at=_utcnow(),
                note="重复的关闭请求：终端已关闭，不再产生副作用（记录保留）。",
            )
        session = self._sessions.get(terminal_id)
        if session is None:
            # PTY 输入端不能恢复；进程身份足够时仍可显式关闭残留进程组。
            pid = record["pid"]
            pgid = record["pgid"]
            same_host = bool(record["host_boot_id"]) and record["host_boot_id"] == self._host_boot_id()
            rebooted = bool(record["host_boot_id"]) and not same_host
            confirmed = rebooted or (pgid is not None and not self._group_alive(int(pgid)))
            identified = (
                same_host and pid is not None and pgid == pid
                and pid_is_alive(int(pid), expected_start_ticks=record["pid_start_ticks"])
            )
            if not confirmed and identified and pgid is not None:
                self._signal_group(int(pgid), signal.SIGTERM)
                deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
                while time.monotonic() < deadline and self._group_alive(int(pgid)):
                    await asyncio.sleep(0.1)
                if self._group_alive(int(pgid)) and escalate:
                    self._signal_group(int(pgid), signal.SIGKILL)
                    deadline = time.monotonic() + KILL_CONFIRM_SECONDS
                    while time.monotonic() < deadline and self._group_alive(int(pgid)):
                        await asyncio.sleep(0.1)
                confirmed = not self._group_alive(int(pgid))
            await self._ledger.update_terminal(
                terminal_id, state="closed" if confirmed else "unknown",
                interaction_state="lost", closed_at=_utcnow() if confirmed else None,
                termination_reason="user_cancelled" if confirmed else "unknown",
            )
            return proto.OperationReceipt(
                operation_id=envelope.operation_id, action="terminal_close",
                status="succeeded" if confirmed else "unknown",
                terminal_id=terminal_id, resource_id=terminal_id,
                process_state=proto.ResourceStatus.CLOSED if confirmed else proto.ResourceStatus.UNKNOWN,
                process_confirmed_exited=confirmed, interaction_state=proto.InteractionState.LOST,
                accepted_at=_utcnow(),
                note=("交互已丢失；残留进程组已确认退出，历史记录保留。" if confirmed else
                      "交互已丢失，进程组身份或退出无法确认；未确认关闭，不自动重建或重发输入。"),
            )

        session.closed = True
        confirmed = False
        detail = ""
        if session.exited and not self._group_alive(session.pid):
            confirmed = True
            detail = "进程此前已退出"
        else:
            self._signal_group(session.pid, signal.SIGTERM)
            deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not self._group_alive(session.pid):
                    confirmed = True
                    break
                await asyncio.sleep(0.2)
            if not confirmed and escalate:
                self._signal_group(session.pid, signal.SIGKILL)
                deadline = time.monotonic() + KILL_CONFIRM_SECONDS
                while time.monotonic() < deadline:
                    if not self._group_alive(session.pid):
                        confirmed = True
                        break
                    await asyncio.sleep(0.2)
            detail = "SIGTERM/SIGKILL 后已确认退出" if confirmed else "无法确认进程组退出"
        if confirmed and not session.exited:
            # 让 reap 循环完成落库；这里直接给出终态兜底
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._await_exit(session), timeout=3.0)
        await self._ledger.update_terminal(
            terminal_id,
            state="closed" if confirmed else "unknown",
            closed_at=_utcnow() if confirmed else None,
            termination_reason="user_cancelled" if confirmed else "unknown",
        )
        await self._emit(
            proto.ExecutionEventType.RESOURCE_CLOSED.value,
            session,
            {"confirmed": confirmed, "detail": detail},
        )
        return proto.OperationReceipt(
            operation_id=envelope.operation_id,
            action="terminal_close",
            status="succeeded" if confirmed else "unknown",
            process_confirmed_exited=confirmed,
            terminal_id=terminal_id,
            resource_id=terminal_id,
            process_state=(
                proto.ResourceStatus.CLOSED if confirmed else proto.ResourceStatus.UNKNOWN
            ),
            interaction_state=proto.InteractionState.NOT_APPLICABLE,
            termination_reason=(
                proto.TerminationReason.USER_CANCELLED if confirmed else proto.TerminationReason.UNKNOWN
            ),
            accepted_at=_utcnow(),
            note=(
                f"关闭回执幂等；记录与日志保留。{detail}"
                if confirmed
                else f"关闭请求已发出但未确认进程退出：{detail}；按 unknown 展示，不夸大已停止。"
            ),
        )

    async def _await_exit(self, session: _Session) -> None:
        while not session.exited:
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ #
    # 恢复核对与关停
    # ------------------------------------------------------------------ #

    async def reconcile(self) -> dict[str, Any]:
        """服务启动核对：不与本 boot 关联的终端交互一律标记丢失。"""
        summary: dict[str, Any] = {"checked": 0, "interaction_lost": [], "processes": {}}
        for row in await self._ledger.non_terminal_terminals():
            summary["checked"] += 1
            terminal_id = str(row["terminal_id"])
            pid, pgid = row["pid"], row["pgid"]
            same_host = bool(row["host_boot_id"]) and row["host_boot_id"] == self._host_boot_id()
            rebooted = bool(row["host_boot_id"]) and not same_host
            gone = rebooted or (pgid is not None and not self._group_alive(int(pgid)))
            identified = same_host and pid is not None and pid_is_alive(
                int(pid), expected_start_ticks=row["pid_start_ticks"]
            )
            alive = not gone and identified
            state = "exited" if gone else "running" if alive else "unknown"
            summary["processes"][terminal_id] = state
            await self._ledger.update_terminal(
                terminal_id, interaction_state="lost", state=state,
                service_boot_id=self._boot_id, exited_at=_utcnow() if gone else None,
                termination_reason="unknown" if gone else None,
            )
            await self._ledger.append_event(
                event_id=str(uuid4()),
                type=proto.ExecutionEventType.INTERACTION_LOST.value,
                session_id=str(row["session_id"]),
                resource_id=terminal_id,
                operation_id=row["operation_id"],
                payload={
                    "process_alive": alive,
                    "note": (
                        "Executor 重启：交互能力已丢失。保留记录并核对残留进程；"
                        "不自动重建终端、不自动重发输入。"
                    ),
                },
            )
            summary["interaction_lost"].append(terminal_id)
        return summary

    async def shutdown(self) -> None:
        """服务关停：**不杀任务**。只清理本进程持有的 reader 与内存对象。"""
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(session.master_fd)
            if session.reap_task is not None:
                session.reap_task.cancel()
            session.matcher.close()
            with contextlib.suppress(Exception):
                session.store.flush_index(force=True)

    def active_count(self) -> int:
        return sum(1 for item in self._sessions.values() if not item.exited)

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    async def _emit(self, event_type: str, session: _Session, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._ledger.append_event(
                event_id=str(uuid4()),
                type=event_type,
                session_id=session.session_id,
                resource_id=session.terminal_id,
                operation_id=session.operation_id,
                payload=payload,
            )
