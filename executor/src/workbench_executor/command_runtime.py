"""非交互命令任务的受理、恢复与终止（Executor 服务侧）。

职责边界（规格 §7）：
- **受理**：校验 fence/配额/参数 -> 先持久化操作与任务记录 -> 原子写执行规范 ->
  启动独立包装进程（`job_worker`，新会话，脱离服务生命周期）；
- **恢复核对**：服务重启后按台账与不可变回执核对，**不自动重跑未知任务**；
  只有"可证明未执行"（没有 start.json 的陈旧 claim）才允许重新启动包装进程；
- **取消**：写持久取消请求（任务目录中的控制文件），等待完成回执；
  未确认一律返回 unknown，绝不把"已请求"显示成"已停止"；
- **输出读取**：直接读取保留日志（包装进程持续写入），游标绑定资源。

真实执行状态归 Executor 台账与不可变回执；本模块不做第二份状态机。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from workbench_executor._generated.contracts import execution_runtime as proto
from workbench_executor.output_store import (
    CursorError,
    OutputIndex,
    decode_chunk,
    encode_cursor,
    missing_ranges_for,
    parse_cursor,
    read_slice,
    stream_path,
)
from workbench_executor.processes import minimal_environment
from workbench_executor.settings import ExecutorSettings

#: 取消确认窗口：TERM 宽限 + KILL 确认（与包装进程内部保持一致）。
CANCEL_CONFIRM_SECONDS = 16.0
#: 等待完成回执的轮询间隔。
CONFIRM_POLL_SECONDS = 0.25


class RuntimeRejected(Exception):
    """确定拒绝（参数/配额/fence/能力）。上游必须返回 4xx，并如实说明原因。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


class CommandRuntime:
    def __init__(self, settings: ExecutorSettings, ledger: Any, boot_id: str) -> None:
        self._settings = settings
        self._ledger = ledger
        self._boot_id = boot_id
        self._spawned: dict[str, asyncio.subprocess.Process] = {}

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #

    @property
    def jobs_root(self) -> Path:
        return self._settings.jobs_root

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def session_workspace(self, workspace_id: str) -> Path:
        from workbench_executor.filesystem import workspace_for

        return workspace_for(self._settings.workspace_root, workspace_id).root

    # ------------------------------------------------------------------ #
    # 受理
    # ------------------------------------------------------------------ #

    async def accept(
        self,
        envelope: proto.ExecutionOperationEnvelope,
        *,
        arguments: dict[str, Any],
        kind: str,
        purpose: str = "",
    ) -> proto.OperationReceipt:
        """受理一个非交互任务（command / script / monitor 共用内核）。"""
        existing = await self._ledger.get_operation(envelope.operation_id)
        if existing is not None:
            if existing.request_digest != envelope.request_digest:
                raise RuntimeRejected(
                    "operation_conflict",
                    "同一 operation_id 复用了不同参数；显式重做必须生成新的逻辑操作 ID",
                    details={"operation_id": envelope.operation_id},
                )
            return self._receipt_from_record(existing, deduplicated=True)

        session_id = envelope.session_id
        job_id = str(uuid4())

        # 配额与磁盘预检（拒绝新建，不淘汰已有资源）。
        active_total, active_pty = await self._ledger.count_active_for_session(session_id)
        if active_total >= self._settings.session_max_resources:
            raise RuntimeRejected(
                "resource_limit",
                f"该会话并行资源已达上限（{self._settings.session_max_resources}）；"
                "已有资源不会被自动淘汰，请先关闭不再需要的资源。",
                details={"active": active_total, "limit": self._settings.session_max_resources},
            )
        _ = active_pty
        global_active = await self._ledger.count_active_global()
        if global_active >= self._settings.global_max_resources:
            raise RuntimeRejected(
                "resource_limit",
                f"执行端全局并行资源已达上限（{self._settings.global_max_resources}）",
                details={"active": global_active, "limit": self._settings.global_max_resources},
            )
        self._ensure_disk_headroom()

        # 输出预算预留：per-resource 上限与 session 剩余额度取小。
        used = await self._ledger.session_budget_used(session_id)
        remaining = self._settings.session_log_budget_bytes - used
        if remaining < self._settings.min_output_reservation_bytes:
            raise RuntimeRejected(
                "resource_limit",
                "该会话采集日志预算已耗尽；已有日志不会被删除，请先行处理。",
                details={"used": used, "budget": self._settings.session_log_budget_bytes},
            )
        reservation = min(self._settings.resource_log_limit_bytes, remaining)
        try:
            await self._ledger.reserve_output_budget(
                resource_id=job_id,
                session_id=session_id,
                bytes_needed=reservation,
                session_budget=self._settings.session_log_budget_bytes,
            )
        except ValueError as exc:
            raise RuntimeRejected("resource_limit", str(exc)) from exc

        job_directory = self.job_dir(job_id)
        job_directory.mkdir(parents=True, exist_ok=True)
        try:
            spec = self._build_spec(envelope, arguments, kind, job_id, purpose, reservation)
            self._write_spec(job_directory, spec)
            await self._ledger.insert(
                job_id=job_id,
                operation_id=envelope.operation_id,
                request_digest=envelope.request_digest,
                kind=kind,
                payload=self._ledger_payload(arguments),
                state="received",
                session_id=session_id,
                run_id=envelope.run_id,
                runtime="execution_v1",
                spec_hash=spec["spec_hash"],
                output_limit_action=str(spec["output_limit_action"]),
                output_limit_bytes=int(spec["output_limit_bytes"]),
                purpose=purpose,
                deadline_at=spec.get("deadline_at"),
                watch_json=json.dumps(spec.get("watch") or [], ensure_ascii=False),
                delegation_id=envelope.delegation_id,
                delegation_revision=envelope.delegation_revision,
            )
            await self._ledger.insert_operation(
                operation_id=envelope.operation_id,
                session_id=session_id,
                run_id=envelope.run_id,
                tool_call_id=envelope.tool_call_id,
                request_digest=envelope.request_digest,
                action=envelope.action,
                resource_id=job_id,
                owner_generation=envelope.owner_generation,
                dispatch_control_version=envelope.dispatch_control_version,
                session_control_seq=envelope.session_control_seq,
                delegation_id=envelope.delegation_id,
                delegation_revision=envelope.delegation_revision,
                receipt={"resource_id": job_id, "state": "received"},
            )
        except Exception:
            # 受理半途失败：释放预算并保留明确错误，不留悬挂资源。
            await self._ledger.release_output_budget(job_id)
            raise

        await self._ledger.append_event(
            event_id=str(uuid4()),
            type=proto.ExecutionEventType.RESOURCE_RECEIVED.value,
            session_id=session_id,
            resource_id=job_id,
            operation_id=envelope.operation_id,
            payload={"kind": kind, "purpose": purpose},
        )
        spawned = await self._spawn_worker(job_id)
        if not spawned:
            await self._ledger.update(
                job_id,
                state="failed",
                error_code="worker_spawn_failed",
                error_message="包装进程启动失败",
                finished_at=_utcnow(),
            )
            await self._ledger.release_output_budget(job_id)
            await self._ledger.complete_operation(
                envelope.operation_id, status="failed", resource_id=job_id
            )
            raise RuntimeRejected("execution_failed", "包装进程启动失败，任务未执行")

        record = await self._ledger.get(job_id)
        assert record is not None
        return self._receipt_from_job(record, kind=kind, deduplicated=False)

    def _ensure_disk_headroom(self) -> None:
        try:
            usage = shutil.disk_usage(self._settings.data_root)
        except OSError as exc:
            raise RuntimeRejected("resource_limit", f"无法读取磁盘信息：{exc}") from exc
        if usage.free < self._settings.disk_low_watermark_bytes:
            raise RuntimeRejected(
                "resource_limit",
                f"数据盘剩余空间不足（{usage.free} 字节 < 低水位 "
                f"{self._settings.disk_low_watermark_bytes} 字节）；拒绝新建任务。",
            )

    def _ledger_payload(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """台账只保存接收事实与脱敏参数；命令正文不进入公开事件。"""
        payload = dict(arguments)
        for key in ("stdin_text", "code", "password"):
            if key in payload and payload[key] is not None:
                value = str(payload[key])
                payload[key] = f"[REDACTED {len(value)} chars]"
        return payload

    def _build_spec(
        self,
        envelope: proto.ExecutionOperationEnvelope,
        arguments: dict[str, Any],
        kind: str,
        job_id: str,
        purpose: str,
        reservation: int,
    ) -> dict[str, Any]:
        workspace_id = str(arguments.get("workspace_id") or envelope.session_id)
        workspace_root = self.session_workspace(workspace_id)
        workspace_root.mkdir(parents=True, exist_ok=True)
        cwd_raw = str(arguments.get("cwd") or ".")
        cwd = (workspace_root / cwd_raw).resolve() if cwd_raw not in {"", "."} else workspace_root
        if cwd != workspace_root and workspace_root not in cwd.parents:
            raise RuntimeRejected("validation_failed", f"工作目录越界：{cwd_raw}")

        job_dir = self.job_dir(job_id)
        argv, script_snapshot = self._resolve_argv(kind, arguments, job_dir, envelope)
        timeout = arguments.get("execution_timeout_seconds")
        deadline_at = None
        if timeout:
            from datetime import timedelta

            deadline_at = (datetime.now(UTC) + timedelta(seconds=int(timeout))).isoformat()
        spec: dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_id,
            "operation_id": envelope.operation_id,
            "session_id": envelope.session_id,
            "run_id": envelope.run_id,
            "kind": kind,
            "argv": argv,
            "cwd": str(cwd),
            "env": {str(k): str(v) for k, v in (arguments.get("env") or {}).items()},
            "stdin_text": (arguments.get("params_json") if kind == "script" and arguments.get("params_json") is not None else arguments.get("stdin_text")),
            "execution_timeout_seconds": int(timeout) if timeout else None,
            "deadline_at": deadline_at,
            "output_limit_bytes": reservation,
            "output_limit_action": str(arguments.get("output_limit_action") or "preserve_process"),
            "watch": list(arguments.get("watch") or []),
            "purpose": purpose,
            "created_at": _utcnow(),
        }
        if arguments.get("template_hash"):
            spec["template"] = {key: arguments.get(key) for key in ("template_hash", "template_revision", "params")}
        if script_snapshot is not None:
            spec["script_snapshot"] = script_snapshot
        canonical = json.dumps(spec, ensure_ascii=False, sort_keys=True)
        spec["spec_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return spec

    def _resolve_argv(
        self,
        kind: str,
        arguments: dict[str, Any],
        job_dir: Path,
        envelope: proto.ExecutionOperationEnvelope,
    ) -> tuple[list[str], str | None]:
        if kind in {"command", "monitor"}:
            if arguments.get("argv"):
                return [str(item) for item in arguments["argv"]], None
            command = str(arguments.get("command") or "")
            if not command:
                raise RuntimeRejected("validation_failed", "command 与 argv 必须二选一")
            shell = self._settings.shell_path
            # 非登录、非交互：`-c` 只执行给定文本；不使用 `-l` 读取 profile。
            return [shell, "-c", command], None

        if kind == "script":
            language = str(arguments.get("language") or "")
            interpreter = self._interpreter_for(language)
            code = arguments.get("code")
            path = arguments.get("path")
            if (code is not None) == bool(path):
                raise RuntimeRejected("validation_failed", "code 与 path 必须二选一")
            extension = {"python": ".py", "shell": ".sh", "node": ".js"}[language]
            if code is not None:
                content = str(code)
            else:
                # path 必须取当前工作区可读快照：先读入内容，再写入任务目录副本执行，
                # 保证执行的是受理时刻的内容而不是随后被并发修改的文件。
                from workbench_executor.filesystem import workspace_for

                workspace = workspace_for(
                    self._settings.workspace_root,
                    str(arguments.get("workspace_id") or envelope.session_id),
                )
                snapshot = workspace.read_file(str(path), offset=0, limit=4 * 1024 * 1024)
                if snapshot.truncated:
                    raise RuntimeRejected("validation_failed", "脚本文件过大，超过 4 MiB 快照上限")
                if snapshot.text is None:
                    raise RuntimeRejected(
                        "validation_failed", f"脚本不是合法 UTF-8（{snapshot.encoding_note}）"
                    )
                content = snapshot.text
            snapshot_path = self.write_script_snapshot(job_dir, content, extension)
            return [interpreter, snapshot_path], snapshot_path
        raise RuntimeRejected("validation_failed", f"未知任务类型：{kind}")

    def _interpreter_for(self, language: str) -> str:
        if language == "python":
            return self._settings.resolved_python_interpreter
        if language == "shell":
            return self._settings.shell_path
        if language == "node":
            node = shutil.which("node")
            if not node:
                raise RuntimeRejected(
                    "dependency_unavailable", "执行端未发现 node 解释器，无法运行 JS 脚本"
                )
            return node
        raise RuntimeRejected("validation_failed", f"不支持的脚本语言：{language}")

    # ------------------------------------------------------------------ #
    # spec 落盘与包装进程
    # ------------------------------------------------------------------ #

    def _write_spec(self, job_dir: Path, spec: dict[str, Any]) -> None:
        job_dir.mkdir(parents=True, exist_ok=True)
        data = json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2)
        target = job_dir / "spec.json"
        temporary = job_dir / "spec.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        temporary.replace(target)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        sha256_path = job_dir / "spec.sha256"
        fd = os.open(sha256_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (digest + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        spec["_file_digest"] = digest

    def write_script_snapshot(self, job_dir: Path, content: str, extension: str) -> str:
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / f"script{extension}"
        path.write_text(content, encoding="utf-8", newline="\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return str(path)

    async def _spawn_worker(self, job_id: str) -> bool:
        job_dir = self.job_dir(job_id)
        log_path = job_dir / "worker.log"
        env = minimal_environment()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            with log_path.open("ab") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "workbench_executor.job_worker",
                    "--job-id",
                    job_id,
                    "--spec-path",
                    str(job_dir / "spec.json"),
                    "--data-root",
                    str(self._settings.data_root),
                    "--workspace-root",
                    str(self._settings.workspace_root),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    env=env,
                    cwd=str(self._settings.data_root),
                )
        except (OSError, ValueError):
            return False
        self._spawned[job_id] = process
        return True

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #

    async def cancel(
        self, resource_id: str, *, request_id: str, escalate: bool = True
    ) -> dict[str, Any]:
        record = await self._ledger.get(resource_id)
        if record is None:
            raise RuntimeRejected("resource_not_found", f"资源不存在：{resource_id}")
        if record.state in {"succeeded", "failed", "cancelled"}:
            if record.cancel_request_id == request_id:
                return {
                    "state": record.state,
                    "process_confirmed_exited": record.state in {"succeeded", "failed", "cancelled"},
                    "detail": "重复的取消请求；返回同一结论",
                }
            return {
                "state": record.state,
                "process_confirmed_exited": True,
                "detail": "资源已处于终态，取消无额外效果",
            }
        if record.cancel_request_id and record.cancel_request_id != request_id:
            # 不同请求的取消：同样尊重先到者，但返回当前状态
            pass
        control_dir = self.job_dir(resource_id) / "control"
        control_dir.mkdir(parents=True, exist_ok=True)
        control_path = control_dir / "cancel.json"
        payload = {"request_id": request_id, "escalate": escalate, "requested_at": _utcnow()}
        temporary = control_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(control_path)
        await self._ledger.update(
            resource_id,
            cancel_requested=1,
            cancel_request_id=request_id,
        )
        await self._ledger.append_event(
            event_id=str(uuid4()),
            type=proto.ExecutionEventType.CANCEL_REQUESTED.value,
            session_id=record.session_id,
            resource_id=resource_id,
            operation_id=record.operation_id,
            payload={"request_id": request_id},
        )
        confirmed, detail = await self._wait_for_cancel(resource_id)
        return {
            "state": "cancelled" if confirmed else "unknown",
            "process_confirmed_exited": confirmed,
            "detail": detail,
        }

    async def _wait_for_cancel(self, resource_id: str) -> tuple[bool, str]:
        deadline = asyncio.get_running_loop().time() + CANCEL_CONFIRM_SECONDS
        completion_path = self.job_dir(resource_id) / "completion.json"
        while True:
            completion = _read_json(completion_path)
            if completion is not None:
                record = await self._ledger.get(resource_id)
                if record is None or not self._completion_valid(record, completion):
                    return False, "完成回执身份或 hash 无效；退出未确认"
                confirmed = bool(completion.get("group_confirmed_exited"))
                reason = completion.get("termination_reason")
                await self._ledger.append_event(
                    event_id=str(uuid4()),
                    type=proto.ExecutionEventType.CANCEL_CONFIRMED.value,
                    session_id=str(completion.get("session_id") or ""),
                    resource_id=resource_id,
                    operation_id=completion.get("operation_id"),
                    payload={"confirmed": confirmed, "reason": reason},
                )
                return (
                    confirmed,
                    f"完成回执：termination_reason={reason}，"
                    f"group_confirmed_exited={confirmed}",
                )
            record = await self._ledger.get(resource_id)
            if record is not None and record.state in {"succeeded", "failed", "cancelled"}:
                return record.state == "cancelled", "台账已到终态"
            if asyncio.get_running_loop().time() > deadline:
                return False, "在确认窗口内没有收到完成回执；取消未确认，按 unknown 处理"
            await asyncio.sleep(CONFIRM_POLL_SECONDS)

    # ------------------------------------------------------------------ #
    # 恢复核对
    # ------------------------------------------------------------------ #

    async def reconcile(self) -> dict[str, Any]:
        """启动核对：按台账与不可变回执核对，不自动重跑未知任务。"""
        summary: dict[str, Any] = {"checked": 0, "imported": [], "unknown": [], "respawned": []}
        for record in await self._ledger.non_terminal_execution_jobs():
            summary["checked"] += 1
            job_dir = self.job_dir(record.job_id)
            completion = _read_json(job_dir / "completion.json")
            if completion is not None and self._completion_valid(record, completion):
                await self._import_completion(record, completion)
                summary["imported"].append(record.job_id)
                continue
            process = _read_json(job_dir / "process.json")
            worker_alive = False
            if record.worker_pid is not None:
                from workbench_executor.processes import pid_is_alive

                worker_alive = pid_is_alive(
                    record.worker_pid, expected_start_ticks=record.worker_pid_start_ticks
                )
            if process is not None:
                child_alive = False
                if process.get("child_pid") is not None:
                    from workbench_executor.processes import pid_is_alive

                    child_alive = pid_is_alive(
                        int(process["child_pid"]),
                        expected_start_ticks=process.get("child_pid_start_ticks"),
                    )
                if worker_alive or child_alive:
                    if worker_alive:
                        await self._ledger.update(
                            record.job_id, state="running", last_heartbeat=_utcnow()
                        )
                        await self._ledger.append_event(
                            event_id=str(uuid4()),
                            type=proto.ExecutionEventType.RECOVERY_CHECKED.value,
                            session_id=record.session_id,
                            resource_id=record.job_id,
                            operation_id=record.operation_id,
                            payload={
                                "result": "running",
                                "note": "包装进程仍在运行；服务重启不影响任务与截止计时。",
                            },
                        )
                        summary["imported"].append(record.job_id)
                    else:
                        await self._mark_unknown(
                            record,
                            "包装进程缺失但子进程仍在；输出采集已中断，无法确认终态，不自动重跑",
                        )
                        summary["unknown"].append(record.job_id)
                    continue
                # 包装进程与子进程都不在了且没有完成回执：无法取得真实 exit code。
                await self._mark_unknown(
                    record,
                    "服务重启后无法核实该任务终态（无可用完成回执）；未自动重跑",
                )
                summary["unknown"].append(record.job_id)
                continue
            start = _read_json(job_dir / "start.json")
            if start is not None:
                await self._mark_unknown(
                    record,
                    "存在启动回执但缺少子进程回执：启动窗口未知，不自动重跑",
                )
                summary["unknown"].append(record.job_id)
                continue
            claim = _read_json(job_dir / "claim.lock")
            if claim is None and record.spec_hash:
                # 从未 claim：可证明未执行，允许重新启动包装进程。
                if await self._spawn_worker(record.job_id):
                    await self._ledger.append_event(
                        event_id=str(uuid4()),
                        type=proto.ExecutionEventType.RECOVERY_CHECKED.value,
                        session_id=record.session_id,
                        resource_id=record.job_id,
                        operation_id=record.operation_id,
                        payload={
                            "result": "respawned",
                            "note": "未发现 claim/start 证据，确定未执行，已重新启动包装进程。",
                        },
                    )
                    summary["respawned"].append(record.job_id)
                else:
                    await self._mark_unknown(record, "重新启动包装进程失败")
                    summary["unknown"].append(record.job_id)
                continue
            await self._mark_unknown(
                record,
                "存在陈旧 claim 但缺少启动回执：为安全起见不自动重跑，请人工核对",
            )
            summary["unknown"].append(record.job_id)
        return summary

    def _completion_valid(self, record: Any, completion: dict[str, Any]) -> bool:
        receipt_hash = completion.get("receipt_hash")
        if not receipt_hash:
            return False
        payload = {key: value for key, value in completion.items() if key != "receipt_hash"}
        actual = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()
        if actual != receipt_hash:
            return False
        if completion.get("job_id") != record.job_id:
            return False
        if record.spec_hash and completion.get("spec_hash") != record.spec_hash:
            return False
        operation_mismatch = bool(completion.get("operation_id")) and bool(
            completion["operation_id"] != record.operation_id
        )
        return not operation_mismatch

    async def _import_completion(self, record: Any, completion: dict[str, Any]) -> None:
        reason = str(completion.get("termination_reason") or "unknown")
        exit_code = completion.get("exit_code")
        if reason == "user_cancelled":
            state = "cancelled"
        elif reason == "unknown":
            state = "unknown"
        elif exit_code == 0 and reason == "exited_normally":
            state = "succeeded"
        else:
            state = "failed"
        output = completion.get("output") or {}
        await self._ledger.update(
            record.job_id,
            state=state,
            exit_code=exit_code,
            finished_at=completion.get("finished_at") or _utcnow(),
            termination_reason=reason,
            dropped_bytes=int(completion.get("dropped_bytes") or 0),
            completion_path=str(self.job_dir(record.job_id) / "completion.json"),
            cancel_confirmed=int(reason in {
                "user_cancelled", "execution_timeout", "output_limit_terminate"
            } and bool(completion.get("group_confirmed_exited"))),
            byte_count=sum(int(item.get("stored_bytes", 0)) for item in output.values()),
        )
        await self._ledger.settle_output_budget(
            record.job_id, sum(int(item.get("stored_bytes") or 0) for item in output.values())
        )
        await self._ledger.append_event(
            event_id=str(uuid4()),
            type=proto.ExecutionEventType.RESOURCE_EXITED.value,
            session_id=record.session_id,
            resource_id=record.job_id,
            operation_id=record.operation_id,
            payload={
                "state": state,
                "exit_code": exit_code,
                "termination_reason": reason,
                "imported_after_restart": True,
            },
        )

    async def _mark_unknown(self, record: Any, note: str) -> None:
        await self._ledger.update(
            record.job_id,
            state="unknown",
            error_code="uncertain_after_restart",
            error_message=note,
        )
        await self._ledger.append_event(
            event_id=str(uuid4()),
            type=proto.ExecutionEventType.RECOVERY_CHECKED.value,
            session_id=record.session_id,
            resource_id=record.job_id,
            operation_id=record.operation_id,
            payload={"result": "unknown", "note": note},
        )

    # ------------------------------------------------------------------ #
    # 状态与输出
    # ------------------------------------------------------------------ #

    def _receipt_from_record(
        self, record: Any, *, deduplicated: bool
    ) -> proto.OperationReceipt:
        record_job = getattr(record, "resource_id", None)
        return proto.OperationReceipt(
            operation_id=record.operation_id,
            action=record.action,
            status=record.status,
            resource_id=record_job,
            process_state=proto.ResourceStatus.RUNNING
            if record.status in {"accepted", "executed"}
            else None,
            deduplicated=deduplicated,
            accepted_at=record.accepted_at,
            executed_at=record.executed_at,
            note="该 operation_id 已受理，返回原记录（不重复执行）",
        )

    def _receipt_from_job(
        self, record: Any, *, kind: str, deduplicated: bool
    ) -> proto.OperationReceipt:
        state = proto.ResourceStatus(record.state) if record.state in {
            "starting",
            "running",
            "exited",
            "closed",
            "unknown",
        } else proto.ResourceStatus.RUNNING
        return proto.OperationReceipt(
            operation_id=record.operation_id,
            action=f"run_{kind}" if kind != "monitor" else "start_monitor",
            status="accepted",
            resource_id=record.job_id,
            job_id=record.job_id,
            process_state=state,
            interaction_state=proto.InteractionState.NOT_APPLICABLE,
            deduplicated=deduplicated,
            accepted_at=record.created_at,
            note=(
                "任务已受理并在独立包装进程中运行；等待超时只返回该任务，不会重新启动。"
                if not deduplicated
                else "幂等重放：返回原任务"
            ),
        )

    async def status_for(self, resource_id: str) -> proto.ResourceStatusView | None:
        record = await self._ledger.get(resource_id)
        if record is None or not record.is_execution_resource:
            return None
        return self._status_from_job(record)

    def _status_from_job(self, record: Any) -> proto.ResourceStatusView:
        state = record.state
        mapping = {
            "received": proto.ResourceStatus.STARTING,
            "starting": proto.ResourceStatus.STARTING,
            "running": proto.ResourceStatus.RUNNING,
            "succeeded": proto.ResourceStatus.EXITED,
            "failed": proto.ResourceStatus.EXITED,
            "cancelled": proto.ResourceStatus.EXITED,
            "unknown": proto.ResourceStatus.UNKNOWN,
        }
        output_index = OutputIndex.load(
            self.job_dir(record.job_id) / "output-index.json", resource_id=record.job_id
        )
        dropped = (
            sum(entry.missing_total_bytes for entry in output_index.streams.values())
            if output_index is not None
            else record.dropped_bytes
        )
        total_stored = (
            sum(entry.stored_bytes for entry in output_index.streams.values())
            if output_index is not None
            else record.byte_count
        )
        return proto.ResourceStatusView(
            resource_id=record.job_id,
            session_id=record.session_id,
            run_id=record.run_id,
            kind=proto.ResourceKind(record.kind),
            state=mapping.get(state, proto.ResourceStatus.UNKNOWN),
            interaction_state=proto.InteractionState.NOT_APPLICABLE,
            purpose=record.purpose,
            created_at=record.created_at,
            started_at=record.started_at,
            exited_at=record.finished_at,
            exit_code=record.exit_code,
            termination_reason=(
                proto.TerminationReason(record.termination_reason)
                if record.termination_reason
                in {item.value for item in proto.TerminationReason}
                else None
            ),
            deadline_at=record.deadline_at,
            output_limit_action=proto.OutputLimitAction(record.output_limit_action),
            output_bytes=int(total_stored),
            dropped_bytes=int(dropped),
            output_limit_bytes=int(record.output_limit_bytes),
            cancel_requested=record.cancel_requested,
            cancel_confirmed=record.cancel_confirmed,
            last_checked_at=_utcnow(),
            notes=["采集日志超限时默认保活；缺失区间在输出页中标注。"]
            if record.cancel_requested and not record.cancel_confirmed
            else [],
        )

    async def read_output(
        self,
        resource_id: str,
        *,
        stream: str,
        cursor: str | None,
        limit: int,
    ) -> proto.OutputPageV2:
        record = await self._ledger.get(resource_id)
        if record is None or not record.is_execution_resource:
            raise RuntimeRejected("resource_not_found", f"资源不存在：{resource_id}")
        try:
            resolved_stream, offset = parse_cursor(
                cursor, resource_id=resource_id, default_stream=stream
            )
        except CursorError as exc:
            raise RuntimeRejected("invalid_cursor", str(exc)) from exc
        if resolved_stream not in {"stdout", "stderr"}:
            raise RuntimeRejected("validation_failed", f"不支持的输出 stream：{resolved_stream}")
        job_dir = self.job_dir(resource_id)
        path = stream_path(job_dir, resolved_stream)
        data, size = read_slice(path, offset=offset, limit=limit)
        decoded = decode_chunk(data)
        if decoded.consumed < len(data) and offset + len(data) < size:
            # 页尾正好切在多字节字符中间：向前补读少量字节把它凑完整，
            # 避免下一页反复读到同一个不完整字符（游标按原始字节推进）。
            extra, size = read_slice(path, offset=offset + len(data), limit=4)
            if extra:
                data = data + extra
                decoded = decode_chunk(data)
        next_offset = offset + decoded.consumed
        index = OutputIndex.load(job_dir / "output-index.json", resource_id=resource_id)
        processed_state = record.state not in {"received", "starting", "running"}
        missing = missing_ranges_for(
            index, resolved_stream, stored_start=0,
            stored_end=(index.streams[resolved_stream].stored_bytes if index else next_offset),
        )
        entry = index.streams.get(resolved_stream) if index is not None else None
        source_start = offset + (entry.dropped_before(offset) if entry else 0)
        source_end = next_offset + (entry.dropped_before(next_offset) if entry else 0)
        return proto.OutputPageV2(
            resource_id=resource_id,
            stream=resolved_stream,
            cursor=cursor,
            next_cursor=encode_cursor(resource_id, resolved_stream, next_offset),
            byte_start=source_start,
            byte_end=source_end,
            text=decoded.text,
            byte_count=decoded.consumed,
            at_current_end=next_offset >= size,
            process_exited=processed_state,
            truncated=bool(index.limit_reached) if index else record.truncated,
            missing_ranges=[
                proto.ByteRange(start=start, end=end, reason=reason)
                for start, end, reason in missing
            ],
            encoding_error=decoded.error_detail or None,
            note=(
                "at_current_end 只表示已读到当前末尾，不代表程序已结束。"
                if next_offset >= size
                else ""
            ),
        )

    async def list_session_resources(
        self, session_id: str, *, limit: int, offset: int
    ) -> proto.ResourceListPage:
        items, total = await self._ledger.list_session_resources(
            session_id, limit=limit, offset=offset
        )
        views: list[proto.ResourceStatusView] = []
        for item in items:
            if item["kind"] == "job":
                record = item["record"]
                if record.is_execution_resource:
                    views.append(self._status_from_job(record))
            # 终端由 terminals 管理器提供更细的视图；此处跳过，避免两处状态漂移。
        active, pty = await self._ledger.count_active_for_session(session_id)
        return proto.ResourceListPage(
            executor_id=self._settings.executor_id,
            session_id=session_id,
            resources=views,
            next_cursor=str(offset + limit) if offset + limit < total else None,
            total=total,
            session_quota={
                "active": active,
                "active_pty": pty,
                "max_resources": self._settings.session_max_resources,
                "log_budget_bytes": self._settings.session_log_budget_bytes,
                "log_budget_used_bytes": await self._ledger.session_budget_used(session_id),
            },
        )

    async def resource_log_export(self, resource_id: str) -> dict[str, Any]:
        """导出运行中日志/最终工件：不可变快照 + 区间 + hash。"""
        record = await self._ledger.get(resource_id)
        if record is None or not record.is_execution_resource:
            raise RuntimeRejected("resource_not_found", f"资源不存在：{resource_id}")
        job_dir = self.job_dir(resource_id)
        artifacts: list[dict[str, Any]] = []
        for name in ("stdout.log", "stderr.log"):
            path = job_dir / name
            if not path.exists():
                continue
            data = path.read_bytes()
            artifacts.append(
                {
                    "name": name,
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "snapshot_at": _utcnow(),
                    "note": "运行中快照：区间为读取时刻的已存字节；最终工件以 completion.json 为准。",
                }
            )
        completion = _read_json(job_dir / "completion.json")
        return {
            "resource_id": resource_id,
            "artifacts": artifacts,
            "completion": completion,
            "state": record.state,
        }
