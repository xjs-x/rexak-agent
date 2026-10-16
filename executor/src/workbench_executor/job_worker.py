"""非交互任务包装进程：`python -m workbench_executor.job_worker --job-id ... --spec-path ...`

生命周期（规格 §7）：

1. 独占 claim（原子创建 `claim.lock`）—— 并发提交/服务重启/重复启动不得出现两个实际命令；
2. 写不可变 `start.json`（claim 之后、spawn 之前）；
3. 创建命令的独立进程组；写不可变 `process.json`（child PID / start_ticks / PGID / host boot ID）；
4. stdout/stderr 并发排空，执行有界采集与 watch 检测；
5. 截止计时与取消控制由**本进程**负责，Executor 离线不影响；
6. 取得真实 exit code 后原子写 `completion.json`（不可变执行证据），
   再幂等导入 SQLite 台账（单一事实：完成回执），并释放输出预算；
7. 崩溃/重启后由 Executor 按台账与不可变回执核对；无足够证据 -> unknown，不自动重跑。

约定：本进程**不读取**控制端任何凭据，只接受显式 CLI 参数与任务 spec；
本模块面向 Linux 执行岛（`os.killpg` / `os.getpgid` 等仅 POSIX 存在），
平台差异由预检如实报告，**不是**代码缺陷。
"""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 允许以 `python -m workbench_executor.job_worker` 直接运行（包内导入）。
from workbench_executor.ledger import Ledger, LedgerMigrationError
from workbench_executor.output_store import (
    OutputLimitExceeded,
    ResourceOutputStore,
)
from workbench_executor.processes import (
    minimal_environment,
    pid_is_alive,
    process_group_alive,
    read_pid_start_ticks,
)
from workbench_executor.watch import WatchMatcher, build_rules

TERMINATE_GRACE_SECONDS = 10.0
KILL_CONFIRM_SECONDS = 5.0
CANCEL_POLL_SECONDS = 0.2
INDEX_FLUSH_SECONDS = 1.0
#: 事件类型（与协议枚举一致；这里用字符串常量避免 import 生成物带来耦合）。
EVENT_EXITED = "resource.exited"
EVENT_FAILED = "resource.failed"
EVENT_WATCH = "resource.watch_match"
EVENT_LIMIT = "output.limit_reached"
EVENT_WARNING = "resource.warning"


class WorkerError(RuntimeError):
    """包装进程的确定错误（写回 completion.json，状态 failed/failure）。"""


def host_boot_id() -> str:
    """操作系统启动标识。与服务实例 boot_id **分开**，两者不能混用。"""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-boot"


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON（tmp + fsync + rename），权限 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def load_spec(
    spec_path: Path, *, job_id: str, jobs_root: Path, workspace_root: Path
) -> tuple[dict[str, Any], str]:
    """加载并校验执行规范，返回 `(spec, 内容哈希)`。

    拒绝路径穿越、符号链接置换与明显身份不一致（规格 §7 末尾）。
    """
    resolved = spec_path.resolve(strict=True)
    if not resolved.is_relative_to(jobs_root.resolve()):
        raise WorkerError(f"spec 路径越界：{resolved}")
    raw_bytes = resolved.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    digest_file = resolved.with_suffix(".sha256")
    expected = digest_file.read_text(encoding="utf-8").strip() if digest_file.exists() else ""
    if expected and expected != digest:
        raise WorkerError("spec 内容哈希与声明不一致（可能被替换）")
    try:
        spec = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"spec 不是合法 UTF-8 JSON：{exc}") from exc
    if not isinstance(spec, dict):
        raise WorkerError("spec 结构非法")
    if spec.get("job_id") != job_id:
        raise WorkerError("spec 的 job_id 与命令行不一致")
    if spec.get("spec_hash"):
        canonical = {key: value for key, value in spec.items() if key != "spec_hash"}
        content_hash = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if spec["spec_hash"] != content_hash:
            raise WorkerError("spec 自声明哈希不匹配")
        # 文件完整性与逻辑规范身份是两个哈希，回执必须沿用台账的逻辑身份。
        digest = content_hash
    cwd = Path(str(spec.get("cwd") or workspace_root)).resolve()
    if not cwd.is_relative_to(workspace_root.resolve()):
        raise WorkerError(f"spec 的工作目录越界：{cwd}")
    spec["cwd"] = str(cwd)
    # 脚本快照：任务目录内的只读副本，再次核对存在性与越界。
    snapshot = spec.get("script_snapshot")
    if snapshot:
        snapshot_path = Path(str(snapshot)).resolve()
        if not snapshot_path.is_relative_to(resolved.parent):
            raise WorkerError("脚本快照路径越界")
        if not snapshot_path.is_file():
            raise WorkerError("脚本快照不存在")
        spec["script_snapshot"] = str(snapshot_path)
    return spec, digest


class Claim:
    """独占启动锁。确保并发提交/重复启动最多产生一个实际命令。"""

    def __init__(self, job_dir: Path) -> None:
        self._path = job_dir / "claim.lock"
        self._identity = {
            "worker_pid": os.getpid(),
            "worker_pid_start_ticks": read_pid_start_ticks(os.getpid()),
            "host_boot_id": host_boot_id(),
            "claimed_at": utcnow(),
        }

    def acquire(self, *, attempts: int = 3) -> bool:
        for _ in range(attempts):
            try:
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if self._holder_alive():
                    return False
                # 持有者已消失且本进程启动前没有 start.json：可证明未执行，允许接管。
                stale = self._path.with_suffix(f".stale.{uuid.uuid4().hex[:8]}")
                with contextlib.suppress(OSError):
                    self._path.rename(stale)
                continue
            try:
                os.write(fd, json.dumps(self._identity).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        return False

    def _holder_alive(self) -> bool:
        holder = read_json(self._path)
        if not holder:
            return False
        pid = holder.get("worker_pid")
        ticks = holder.get("worker_pid_start_ticks")
        if not isinstance(pid, int):
            return False
        return pid_is_alive(pid, expected_start_ticks=ticks if isinstance(ticks, int) else None)

    @property
    def identity(self) -> dict[str, Any]:
        return dict(self._identity)


class JobWorker:
    def __init__(
        self,
        *,
        job_id: str,
        spec: dict[str, Any],
        job_dir: Path,
        data_root: Path,
        spec_hash: str = "",
    ) -> None:
        self.job_id = job_id
        self.spec = spec
        self.job_dir = job_dir
        self.data_root = data_root
        self.spec_hash = spec_hash
        self.ledger = Ledger(data_root / "executor-ledger.sqlite3")
        self.store: ResourceOutputStore | None = None
        self.matcher: WatchMatcher | None = None
        self.control_dir = job_dir / "control"
        self.terminate_reason: str | None = None
        self._child: asyncio.subprocess.Process | None = None
        self._cancel_request: dict[str, Any] | None = None
        self._limit_triggered = False
        self._escalation: asyncio.Task[None] | None = None

    # ---------------- 主流程 ----------------

    async def run(self) -> int:
        spec_hash = self.spec_hash or hashlib.sha256(
            (self.job_dir / "spec.json").read_bytes()
        ).hexdigest()
        start_payload = {
            "job_id": self.job_id,
            "operation_id": self.spec.get("operation_id"),
            "session_id": self.spec.get("session_id"),
            "run_id": self.spec.get("run_id"),
            "kind": self.spec.get("kind"),
            "spec_hash": spec_hash,
            "worker_pid": os.getpid(),
            "worker_pid_start_ticks": read_pid_start_ticks(os.getpid()),
            "host_boot_id": host_boot_id(),
            "claimed_at": utcnow(),
            "phase": "starting",
        }
        atomic_write_json(self.job_dir / "start.json", start_payload)

        await self.ledger.open()
        await self.ledger.update(
            self.job_id,
            state="starting",
            worker_pid=os.getpid(),
            worker_pid_start_ticks=read_pid_start_ticks(os.getpid()),
            host_boot_id=host_boot_id(),
        )
        await self._emit("resource.started", {"job_id": self.job_id, "phase": "starting"})

        argv = [str(item) for item in (self.spec.get("argv") or [])]
        if not argv:
            raise WorkerError("spec 缺少 argv")
        cwd = str(self.spec.get("cwd"))
        env_extra = {
            str(key): str(value) for key, value in (self.spec.get("env") or {}).items()
        }
        env = minimal_environment(env_extra)
        stdin_text = self.spec.get("stdin_text")

        self.store = ResourceOutputStore(
            self.job_dir,
            resource_id=self.job_id,
            limit_bytes=int(self.spec.get("output_limit_bytes") or 0),
            limit_action=str(self.spec.get("output_limit_action") or "preserve_process"),
            index_flush_interval_seconds=INDEX_FLUSH_SECONDS,
        )
        self.store.open()
        rules = build_rules(list(self.spec.get("watch") or []))
        self.matcher = WatchMatcher(rules)

        try:
            self._child = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise WorkerError(f"命令启动失败：{type(exc).__name__}: {exc}") from exc

        child = self._child
        pgid = self._safe_pgid(child.pid)
        process_payload = {
            "job_id": self.job_id,
            "spec_hash": spec_hash,
            "child_pid": child.pid,
            "child_pid_start_ticks": read_pid_start_ticks(child.pid),
            "pgid": pgid,
            "host_boot_id": host_boot_id(),
            "worker_pid": os.getpid(),
            "spawned_at": utcnow(),
            "phase": "running",
        }
        atomic_write_json(self.job_dir / "process.json", process_payload)
        await self.ledger.update(
            self.job_id,
            state="running",
            pid=child.pid,
            pid_start_ticks=read_pid_start_ticks(child.pid),
            started_at=utcnow(),
        )
        await self._emit(
            "resource.started", {"job_id": self.job_id, "pid": child.pid, "pgid": pgid}
        )

        stdin_task = asyncio.create_task(self._feed_stdin(stdin_text))
        drain_stdout = asyncio.create_task(self._drain("stdout", child.stdout))
        drain_stderr = asyncio.create_task(self._drain("stderr", child.stderr))
        wait_task = asyncio.create_task(self._wait_leader(child))
        deadline_task = asyncio.create_task(self._deadline_loop())
        cancel_task = asyncio.create_task(self._cancel_loop())
        index_task = asyncio.create_task(self._index_loop())

        try:
            exit_code = await wait_task
        finally:
            for task in (deadline_task, cancel_task, index_task):
                task.cancel()
            if self._escalation is not None:
                self._escalation.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(deadline_task, cancel_task, index_task, return_exceptions=True)
            if self._escalation is not None:
                with contextlib.suppress(Exception):
                    await asyncio.gather(self._escalation, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(drain_stdout, drain_stderr, return_exceptions=True), timeout=3.0
                )
            await asyncio.gather(stdin_task, return_exceptions=True)
            if self.matcher is not None:
                self.matcher.close()

        group_confirmed, group_note = await self._reap_group(child.pid)
        reason = self._resolve_termination_reason(exit_code, group_confirmed)
        finish_ts = utcnow()
        assert self.store is not None
        self.store.close()

        output_summary = {
            name: {
                "stored_bytes": entry.stored_bytes,
                "source_bytes": entry.source_bytes,
            }
            for name, entry in self.store.index.streams.items()
        }
        completion = {
            "schema_version": 1,
            "job_id": self.job_id,
            "operation_id": self.spec.get("operation_id"),
            "session_id": self.spec.get("session_id"),
            "run_id": self.spec.get("run_id"),
            "kind": self.spec.get("kind"),
            "spec_hash": spec_hash,
            "worker_pid": os.getpid(),
            "child_pid": child.pid,
            "child_pid_start_ticks": process_payload["child_pid_start_ticks"],
            "pgid": pgid,
            "host_boot_id": host_boot_id(),
            "started_at": process_payload["spawned_at"],
            "finished_at": finish_ts,
            "exit_code": exit_code,
            "termination_reason": reason,
            "group_confirmed_exited": group_confirmed,
            "group_note": group_note,
            "cancel_request_id": (self._cancel_request or {}).get("request_id"),
            "output": output_summary,
            "dropped_bytes": sum(
                entry.missing_total_bytes for entry in self.store.index.streams.values()
            ),
            "watch_hits": self.matcher.hit_count if self.matcher is not None else 0,
            "watch_rules": self.matcher.rules_state() if self.matcher is not None else [],
        }
        completion["receipt_hash"] = hashlib.sha256(
            json.dumps(completion, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        atomic_write_json(self.job_dir / "completion.json", completion)
        await self._apply_completion(completion, output_summary)
        return 0

    # ---------------- 子步骤 ----------------

    @staticmethod
    async def _wait_leader(child: asyncio.subprocess.Process) -> int:
        # Process.wait 还会等继承管道的孙进程 EOF；先观察主进程退出才能回收残留组。
        while child.returncode is None:
            await asyncio.sleep(0.05)
        return child.returncode

    async def _feed_stdin(self, stdin_text: Any) -> None:
        child = self._child
        if child is None or child.stdin is None or stdin_text is None:
            return
        try:
            data = str(stdin_text).encode("utf-8")
            # 有界流式写入，避免大输入一次性占用内存。
            for start in range(0, len(data), 65536):
                child.stdin.write(data[start : start + 65536])
                await child.stdin.drain()
            child.stdin.close()
            with contextlib.suppress(Exception):
                await child.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            # 程序提前退出、不再读 stdin：这不是错误，如实记录即可。
            pass

    async def _drain(self, stream: str, reader: asyncio.StreamReader | None) -> None:
        assert self.store is not None
        if reader is None:
            return
        while True:
            try:
                chunk = await reader.read(65536)
            except (ValueError, asyncio.LimitOverrunError):  # pragma: no cover - 防御
                break
            if not chunk:
                break
            await self._consume(stream, chunk)
        # 结束时把残余数据读干净（read(-1) 在这里等价于读到底）
        with contextlib.suppress(Exception):
            tail = await reader.read()
            if tail:
                await self._consume(stream, tail)

    async def _consume(self, stream: str, chunk: bytes) -> None:
        assert self.store is not None
        try:
            self.store.write(stream, chunk)
        except OutputLimitExceeded:
            # 显式 terminate 策略：先记录，再触发终止（排空继续，不丢事件）。
            self._limit_triggered = True
            await self._emit(
                EVENT_LIMIT,
                {
                    "job_id": self.job_id,
                    "stream": stream,
                    "limit_bytes": self.store.index.limit_bytes,
                    "action": "terminate",
                },
            )
            await self._request_termination("output_limit_terminate")
            return
        if self.store.limit_reached and not self._limit_triggered:
            self._limit_triggered = True
            await self._emit(
                EVENT_LIMIT,
                {
                    "job_id": self.job_id,
                    "stream": stream,
                    "limit_bytes": self.store.index.limit_bytes,
                    "action": self.store.index.limit_action,
                    "dropped_bytes": self.store.index.streams[stream].missing_total_bytes,
                    "note": "采集超限：默认保活并持续排空，超额日志以缺失区间记录。",
                },
            )
        if self.matcher is not None and chunk:
            from workbench_executor.output_store import decode_chunk

            decoded = decode_chunk(chunk)
            update = self.matcher.feed(decoded.text)
            for warning in update.warnings:
                await self._emit(EVENT_WARNING, {"job_id": self.job_id, "warning": warning})
            for match in update.matches:
                await self._emit(EVENT_WATCH, {"job_id": self.job_id, **match.to_payload()})

    async def _index_loop(self) -> None:
        warned = False
        while True:
            await asyncio.sleep(INDEX_FLUSH_SECONDS)
            assert self.store is not None
            try:
                self.store.flush_index()
            except OSError as exc:
                if not warned:
                    await self._emit(EVENT_WARNING, {"reason": "output_index_write_failed", "error": str(exc)})
                    warned = True

    async def _deadline_loop(self) -> None:
        raw_deadline = self.spec.get("execution_timeout_seconds")
        if not raw_deadline:
            # 无执行截止：保持挂起，直到取消或进程退出。
            await asyncio.Event().wait()
            return
        deadline_seconds = float(raw_deadline)
        try:
            await asyncio.sleep(deadline_seconds)
        except asyncio.CancelledError:
            raise
        await self._emit(
            EVENT_WARNING,
            {
                "job_id": self.job_id,
                "reason": "execution_timeout",
                "deadline_seconds": deadline_seconds,
            },
        )
        await self._request_termination("execution_timeout")

    async def _cancel_loop(self) -> None:
        """轮询取消控制文件。仅本机、0600 权限、任务目录内。"""
        self.control_dir.mkdir(parents=True, exist_ok=True)
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            request = read_json(self.control_dir / "cancel.json")
            if request and request.get("request_id"):
                if self._cancel_request is None:
                    self._cancel_request = request
                    await self._emit(
                        "resource.cancel_requested",
                        {"job_id": self.job_id, "request_id": request.get("request_id")},
                    )
                await self._request_termination("user_cancelled")

    async def _request_termination(self, reason: str) -> None:
        if self.terminate_reason is None:
            self.terminate_reason = reason
        child = self._child
        if child is None or child.returncode is not None:
            return
        self._signal_group(child.pid, signal.SIGTERM)
        # 忽略 SIGTERM 的程序必须升级为 SIGKILL；升级与主等待解耦，
        # 否则 `wait_task` 会被永远阻塞。
        if self._escalation is None:
            self._escalation = asyncio.create_task(self._escalate(child.pid))

    async def _escalate(self, pid: int) -> None:
        await asyncio.sleep(TERMINATE_GRACE_SECONDS)
        if self._group_alive(pid):
            self._signal_group(pid, signal.SIGKILL)

    async def _reap_group(self, pid: int) -> tuple[bool, str]:
        """确认整个进程组的退出；主进程退出不代表子孙全部退出。"""
        if not self._group_alive(pid):
            return True, "进程组已全部退出"
        self._signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not self._group_alive(pid):
                return True, "SIGTERM 后进程组已退出"
            await asyncio.sleep(0.2)
        self._signal_group(pid, signal.SIGKILL)
        deadline = time.monotonic() + KILL_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            if not self._group_alive(pid):
                return True, "SIGKILL 后进程组已退出"
            await asyncio.sleep(0.2)
        return False, "发送 SIGKILL 后仍无法确认进程组退出"

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return

    @staticmethod
    def _group_alive(pid: int) -> bool:
        return process_group_alive(pid)

    @staticmethod
    def _safe_pgid(pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    def _resolve_termination_reason(self, exit_code: int | None, group_confirmed: bool) -> str:
        if not group_confirmed:
            return "unknown"
        if self.terminate_reason:
            return self.terminate_reason
        if exit_code is None:
            return "unknown"
        return "exited_normally"

    async def _apply_completion(
        self, completion: dict[str, Any], output_summary: dict[str, Any]
    ) -> None:
        """把完成回执幂等导入台账（不可变回执是完成事实的真源）。"""
        reason = str(completion["termination_reason"])
        exit_code = completion.get("exit_code")
        if reason == "exited_normally" and exit_code == 0:
            state = "succeeded"
        elif reason == "user_cancelled":
            state = "cancelled"
        elif reason == "unknown":
            state = "unknown"
        elif exit_code is not None and reason == "exited_normally":
            state = "failed"
        else:
            state = "failed"
        record = await self.ledger.get(self.job_id)
        if record is not None and record.state in {"succeeded", "failed", "cancelled"}:
            # 已终态：只有回执 hash 一致才算幂等重放，否则保留原证据并记录冲突。
            existing = None
            if record.completion_path:
                existing = read_json(Path(record.completion_path))
            if existing and existing.get("receipt_hash") != completion.get("receipt_hash"):
                await self._emit(
                    EVENT_WARNING,
                    {
                        "job_id": self.job_id,
                        "warning": "完成回执与既有终态不一致；保留原回执，不覆盖",
                    },
                )
            return
        await self.ledger.update(
            self.job_id,
            state=state,
            exit_code=exit_code,
            finished_at=completion["finished_at"],
            termination_reason=reason,
            dropped_bytes=int(completion.get("dropped_bytes") or 0),
            completion_path=str(self.job_dir / "completion.json"),
            cancel_confirmed=int(reason in {
                "user_cancelled", "execution_timeout", "output_limit_terminate"
            } and bool(completion.get("group_confirmed_exited"))),
            byte_count=sum(int(item["stored_bytes"]) for item in output_summary.values()),
        )
        # 输出预算在回执导入后释放（重复导入不会重复释放）。
        await self.ledger.settle_output_budget(
            self.job_id, sum(int(item["stored_bytes"]) for item in output_summary.values())
        )
        await self._emit(
            EVENT_EXITED,
            {
                "job_id": self.job_id,
                "state": state,
                "exit_code": exit_code,
                "termination_reason": reason,
                "group_confirmed_exited": bool(completion.get("group_confirmed_exited")),
                "dropped_bytes": int(completion.get("dropped_bytes") or 0),
            },
        )

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """写持久事件。事件写失败不能让执行结论丢失，但必须记录到 stderr。"""
        try:
            await self.ledger.append_event(
                event_id=str(uuid.uuid4()),
                type=event_type,
                session_id=str(self.spec.get("session_id") or ""),
                resource_id=self.job_id,
                operation_id=self.spec.get("operation_id"),
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover - 磁盘故障路径
            print(f"事件写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    data_root = Path(args.data_root).resolve()
    jobs_root = data_root / "jobs"
    workspace_root = Path(args.workspace_root).resolve()
    job_dir = (jobs_root / args.job_id).resolve()
    if not job_dir.is_relative_to(jobs_root):
        raise WorkerError("job 目录越界")
    try:
        spec, digest = load_spec(
            Path(args.spec_path),
            job_id=args.job_id,
            jobs_root=jobs_root,
            workspace_root=workspace_root,
        )
    except (OSError, WorkerError) as exc:
        # spec 阶段失败：写失败回执（不可变），让 Executor 能投影确定终态。
        minimal = {"job_id": args.job_id, "session_id": "", "run_id": "", "kind": "command"}
        worker = JobWorker(
            job_id=args.job_id, spec=minimal, job_dir=job_dir, data_root=data_root
        )
        await _record_failure(worker, f"spec 校验失败：{exc}", "failure")
        print(f"job_worker spec 校验失败：{exc}", file=sys.stderr)
        return 2

    claim = Claim(job_dir)
    if not claim.acquire():
        # 另一个包装进程持有 claim：本进程不得启动第二个实际命令。
        print(json.dumps({"job_id": args.job_id, "skipped": "claim_held"}))
        return 0
    # 持有 claim 后再次核对不可变回执：已完成的任务不重跑。
    if read_json(job_dir / "completion.json") is not None:
        print(json.dumps({"job_id": args.job_id, "skipped": "completion_exists"}))
        return 0

    worker = JobWorker(
        job_id=args.job_id,
        spec=spec,
        job_dir=job_dir,
        data_root=data_root,
        spec_hash=digest,
    )
    try:
        return await worker.run()
    except WorkerError as exc:
        await _record_failure(worker, str(exc), "failure")
        print(f"job_worker 确定失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 未知故障：写失败回执，保留诊断
        await _record_failure(worker, f"{type(exc).__name__}: {exc}", "failure")
        print(f"job_worker 未预期异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        if worker.store is not None:
            worker.store.close()
        if worker.matcher is not None:
            worker.matcher.close()
        await worker.ledger.close()


async def _record_failure(worker: JobWorker, message: str, reason: str) -> None:
    """启动失败/内部异常也要留下回执与终态，绝不悬挂。"""
    child = worker._child
    confirmed, group_note = (await worker._reap_group(child.pid)) if child is not None else (True, "未启动子进程")
    payload = {
        "schema_version": 1,
        "job_id": worker.job_id,
        "operation_id": worker.spec.get("operation_id"),
        "session_id": worker.spec.get("session_id"),
        "run_id": worker.spec.get("run_id"),
        "kind": worker.spec.get("kind"),
        "spec_hash": worker.spec_hash,
        "worker_pid": os.getpid(),
        "child_pid": child.pid if child else None,
        "child_pid_start_ticks": None,
        "pgid": None,
        "host_boot_id": host_boot_id(),
        "started_at": utcnow(),
        "finished_at": utcnow(),
        "exit_code": None,
        "termination_reason": reason,
        "group_confirmed_exited": confirmed,
        "group_note": group_note,
        "cancel_request_id": None,
        "output": {},
        "dropped_bytes": 0,
        "watch_hits": 0,
        "watch_rules": [],
        "error": message,
    }
    payload["receipt_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    with contextlib.suppress(Exception):
        atomic_write_json(worker.job_dir / "completion.json", payload)
    with contextlib.suppress(Exception):
        await worker.ledger.open()
        await worker.ledger.update(
            worker.job_id,
            state="failed" if confirmed else "unknown",
            error_code="worker_start_failed",
            error_message=message,
            finished_at=payload["finished_at"],
            termination_reason=reason,
            completion_path=str(worker.job_dir / "completion.json"),
        )
        await worker.ledger.settle_output_budget(worker.job_id, worker.store.stored_total if worker.store else 0)
        await worker._emit(EVENT_FAILED, {"job_id": worker.job_id, "message": message})
        await worker.ledger.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="非交互任务包装进程")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--spec-path", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--workspace-root", required=True)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except LedgerMigrationError as exc:
        print(f"台账迁移失败，拒绝继续：{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
