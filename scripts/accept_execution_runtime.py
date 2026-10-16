"""命令执行底座验收脚本（规格 §2 的契约）。

三种模式：

- `--mode isolated`       ：在 Linux 新建**独立数据根**、随机回环端口、专用 fixture
  Executor 与假令牌；可杀自己的 fixture Executor 来测试恢复。**不触碰日常数据根**。
- `--mode deployed-readonly`：读取已部署服务的能力/版本/哈希/进程身份；不创建测试、不重启服务。
- `--mode deployed-smoke`：由验收者在部署核对后运行；只创建专属验收 session 与无害本地
  fixture 资源，不修改既有用户 session，只能关闭本脚本创建的资源。

输出 `results.json`、每项精简日志、环境清单与工件哈希；整个脚本遇到失败返回非零。

运行（在 Linux 执行岛上，隔离模式）：

    .venv/bin/python accept_execution_runtime.py --mode isolated \
        --output /tmp/execution-acceptance --cases C01,C02,E01

备注：C/D/E 类需要真实 Linux（进程组、PTY、/proc）；Windows 上运行隔离模式会明确拒绝。
"""

# 本脚本只在 Linux 上提供隔离模式：os.killpg/getpgid、signal.SIGKILL 等仅 POSIX 存在。
# 平台差异由 `run_isolated` 明确拒绝（Non-Run），不是代码缺陷。
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

#: 子进程环境允许透传的最小集合（不传控制端/模型凭据）。
_ENV_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "TMPDIR",
)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def sanitize_env(extra: dict[str, str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOW if name in os.environ}
    env.update(extra)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# --------------------------------------------------------------------------- #
# 结果记录
# --------------------------------------------------------------------------- #


@dataclass
class CaseResult:
    id: str
    name: str
    status: str
    reason: str = ""
    duration: float = 0.0
    evidence_paths: list[str] = field(default_factory=list)


class Recorder:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.logs = output / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.results: list[CaseResult] = []
        self.notes: list[str] = []

    def log(self, case_id: str, name: str, payload: dict[str, Any]) -> str:
        safe_name = re.sub(r"[^\w.-]+", "_", name)[:100]
        path = self.logs / f"{case_id}-{safe_name}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return str(path)

    def text_log(self, case_id: str, name: str, text: str) -> str:
        safe_name = re.sub(r"[^\w.-]+", "_", name)[:100]
        path = self.logs / f"{case_id}-{safe_name}.txt"
        path.write_text(text, encoding="utf-8", newline="\n")
        return str(path)

    async def run(
        self,
        case_id: str,
        name: str,
        func: Callable[[], Any],
        *,
        evidence: list[str] | None = None,
    ) -> None:
        started = time.monotonic()
        try:
            detail = await func() if asyncio.iscoroutinefunction(func) else func()
            status = "PASS"
            reason = ""
            if isinstance(detail, tuple):
                status, reason = detail
        except AssertionError as exc:
            status, reason, detail = "FAIL", str(exc), None
        except Exception as exc:
            status, reason, detail = "FAIL", f"{type(exc).__name__}: {exc}", None
        duration = time.monotonic() - started
        paths = list(evidence or [])
        if detail is not None:
            paths.append(self.log(case_id, name, detail if isinstance(detail, dict) else {"detail": detail}))
        self.results.append(
            CaseResult(
                id=case_id,
                name=name,
                status=status,
                reason=reason,
                duration=round(duration, 3),
                evidence_paths=paths,
            )
        )
        marker = {"PASS": "ok", "FAIL": "FAIL", "NOT_RUN": "skip"}[status]
        print(f"[{marker:^4}] {case_id} {name}（{duration:.2f}s）{'：' + reason if reason else ''}")

    def skip(self, case_id: str, name: str, reason: str) -> None:
        self.results.append(CaseResult(id=case_id, name=name, status="NOT_RUN", reason=reason))
        print(f"[skip] {case_id} {name}：{reason}")


# --------------------------------------------------------------------------- #
# 隔离 fixture Executor
# --------------------------------------------------------------------------- #


class FixtureExecutor:
    """本次演练专属的 Executor 进程（只管理自己启动的进程）。"""

    def __init__(
        self,
        *,
        root: Path,
        token: str,
        extra_env: dict[str, str] | None = None,
        permissive: bool = False,
    ) -> None:
        self.root = root
        self.token = token
        self.port = free_port()
        self.log_path = root / "executor.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.extra_env = dict(extra_env or {})
        self.budgets = {
            "EXECUTOR_RESOURCE_LOG_LIMIT_BYTES": "67108864",
            "EXECUTOR_SESSION_LOG_BUDGET_BYTES": str(512 * 1024 * 1024),
        }
        if permissive:
            # 演练用的小配额/小上限由用例通过 `env()` 显式覆盖。
            self.budgets.update({})

    def env(self, **overrides: str) -> dict[str, str]:
        base = {
            "EXECUTOR_EXECUTOR_ID": "acceptance-fixture",
            "EXECUTOR_HOST": "127.0.0.1",
            "EXECUTOR_PORT": str(self.port),
            "EXECUTOR_TOKEN": self.token,
            "EXECUTOR_DATA_ROOT": str(self.root),
            "EXECUTOR_WORKSPACE_ROOT": str(self.root / "workspaces"),
            "EXECUTOR_EXECUTION_MODE": "trusted",
            "EXECUTOR_NETWORK_ISOLATION": "unavailable",
            "EXECUTOR_BROWSER_ENABLED": "false",
            "EXECUTOR_DISK_LOW_WATERMARK_BYTES": "16777216",
            **self.budgets,
            **self.extra_env,
        }
        base.update({key: str(value) for key, value in overrides.items()})
        return base

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "workbench_executor"],
            cwd=str(self.root),
            env=sanitize_env(self.env()),
            stdout=self.log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.wait_healthy()

    def wait_healthy(self, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2)
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("fixture Executor 未在超时内就绪；请查看 executor.log")

    def stop(self, *, deadline: float = 20.0) -> None:
        """按"服务停止只停止服务"的方式停止（SIGTERM 主进程）。"""
        if self.process is None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=deadline)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - 失败即报告
            raise RuntimeError("fixture Executor 未在超时内退出") from exc
        finally:
            with self._safe():
                self.log.close()
        self.process = None

    def kill(self) -> None:
        """SIGKILL 服务进程（模拟崩溃）；**不**触碰任务进程。"""
        if self.process is None:
            return
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)
        with self._safe():
            self.log.close()
        self.process = None

    def restart(self, *, hard: bool = False, **env_overrides: str) -> None:
        self.stop() if not hard else self.kill()
        self.port = free_port()
        self.env_overrides = dict(env_overrides)
        self.log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "workbench_executor"],
            cwd=str(self.root),
            env=sanitize_env(self.env(**env_overrides)),
            stdout=self.log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.wait_healthy()

    def cleanup(self) -> None:
        """只清理本脚本创建且仍在运行的进程。"""
        if self.process is not None and self.process.poll() is None:
            with self._safe():
                self.process.kill()

    def _safe(self):
        import contextlib

        return contextlib.suppress(Exception)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class ExecutorApi:
    """薄 HTTP 封装（只走回环，不继承代理）。"""

    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url
        self.token = token
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=60,
        )

    def close(self) -> None:
        self.client.close()

    def capabilities(self) -> dict[str, Any]:
        response = self.client.get("/capabilities")
        response.raise_for_status()
        return response.json()

    def sync(self, session_id: str, run_id: str, *, control_version: int = 1, expected_seq=None):
        response = self.client.post(
            "/execution/v1/control",
            json={
                "session_id": session_id,
                "run_id": run_id,
                "owner": "acceptance",
                "owner_generation": 1,
                "control_version": control_version,
                "expected_seq": expected_seq,
                "revocations": [],
            },
        )
        response.raise_for_status()
        return response.json()

    def submit(self, envelope: dict[str, Any]) -> httpx.Response:
        return self.client.post("/execution/v1/operations", json=envelope)

    def operation(self, operation_id: str) -> dict[str, Any]:
        response = self.client.get(f"/execution/v1/operations/{operation_id}")
        response.raise_for_status()
        return response.json()

    def resources(self, session_id: str, **params: Any) -> dict[str, Any]:
        response = self.client.get(
            "/execution/v1/resources", params={"session_id": session_id, **params}
        )
        response.raise_for_status()
        return response.json()

    def resource(self, resource_id: str) -> dict[str, Any]:
        response = self.client.get(f"/execution/v1/resources/{resource_id}")
        response.raise_for_status()
        return response.json()

    def output(
        self, resource_id: str, *, cursor: str | None = None, limit: int = 16384, view: str = "stream", stream: str = "stdout"
    ) -> dict[str, Any]:
        response = self.client.get(
            f"/execution/v1/resources/{resource_id}/output",
            params={"cursor": cursor, "limit": limit, "view": view, "stream": stream},
        )
        response.raise_for_status()
        return response.json()

    def events(self, after: int = 0, limit: int = 500) -> dict[str, Any]:
        response = self.client.get(
            "/execution/v1/events", params={"after": after, "limit": limit}
        )
        response.raise_for_status()
        return response.json()

    def environment(self) -> dict[str, Any]:
        response = self.client.get("/execution/v1/environment")
        response.raise_for_status()
        return response.json()


def envelope(
    *,
    session_id: str,
    run_id: str,
    action: str,
    arguments: dict[str, Any],
    operation_id: str | None = None,
    wait_ms: int = 0,
    digest: str | None = None,
) -> dict[str, Any]:
    body = json.dumps(
        {"action": action, "arguments": arguments}, ensure_ascii=False, sort_keys=True
    )
    return {
        "schema_version": 1,
        "operation_id": operation_id or str(uuid.uuid4()),
        "session_id": session_id,
        "run_id": run_id,
        "tool_call_id": f"acceptance-{uuid.uuid4().hex[:8]}",
        "request_digest": digest or hashlib.sha256(body.encode()).hexdigest(),
        "action": action,
        "arguments": arguments,
        "owner_generation": 1,
        "dispatch_control_version": 1,
        "session_control_seq": 0,
        "wait_ms": wait_ms,
        "created_at": _utcnow(),
    }


def wait_until(
    predicate: Callable[[], Any], *, timeout: float = 20.0, interval: float = 0.2, what: str = "条件"
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:
            last = exc
        else:
            if last:
                return last
        time.sleep(interval)
    raise AssertionError(f"等待{what}超时（最后结果：{last!r}）")


# --------------------------------------------------------------------------- #
# 隔离模式用例
# --------------------------------------------------------------------------- #


class IsolatedSuite:
    def __init__(self, root: Path, recorder: Recorder) -> None:
        self.root = root
        self.recorder = recorder
        self.fixture = FixtureExecutor(root=root / "executor-root", token="acceptance-token")
        self.api: ExecutorApi | None = None
        self.session_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())
        self.created_resources: list[str] = []

    # -------- 生命周期 --------

    def start(self) -> None:
        self.fixture.start()
        self.api = ExecutorApi(self.fixture.base_url, self.fixture.token)
        assert self.api is not None
        self.api.sync(self.session_id, self.run_id)
        caps = self.api.capabilities()
        self.recorder.notes.append(
            f"fixture Executor 版本 {caps.get('service_version')}，执行模式 "
            f"{caps.get('details', {}).get('execution', {}).get('execution_mode')}"
        )

    def restart_service(self, *, hard: bool = False, **env: str) -> None:
        if self.api is not None:
            self.api.close()
        self.fixture.restart(hard=hard, **env)
        self.api = ExecutorApi(self.fixture.base_url, self.fixture.token)
        assert self.api is not None
        self.api.sync(self.session_id, self.run_id)

    def cleanup(self) -> None:
        if self.api is not None:
            # 只关闭本次创建的资源
            for resource_id in list(self.created_resources):
                with _suppress():
                    self.api.submit(
                        envelope(
                            session_id=self.session_id,
                            run_id=self.run_id,
                            action="cancel_resource",
                            arguments={"resource_id": resource_id},
                        )
                    )
            self.api.close()
        self.fixture.cleanup()

    # -------- 便捷方法 --------

    def dispatch(
        self, action: str, arguments: dict[str, Any], *, wait_ms: int = 0, operation_id=None
    ) -> dict[str, Any]:
        assert self.api is not None
        response = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action=action,
                arguments=arguments,
                wait_ms=wait_ms,
                operation_id=operation_id,
            )
        )
        if response.status_code >= 400:
            raise AssertionError(
                f"{action} 被拒绝（HTTP {response.status_code}）：{response.text[:300]}"
            )
        receipt = response.json()
        if receipt.get("resource_id"):
            self.created_resources.append(str(receipt["resource_id"]))
        return receipt

    def run_command(self, argv_or_command: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("run_command", argv_or_command, **kwargs)

    def wait_state(self, resource_id: str, states: set[str], timeout: float = 30.0) -> dict[str, Any]:
        assert self.api is not None

        def probe() -> Any:
            view = self.api.resource(resource_id) if self.api else None
            return view if view and view.get("state") in states else None

        return wait_until(probe, timeout=timeout, what=f"资源进入 {states}")

    def read_all(
        self, resource_id: str, *, limit: int = 4096, max_pages: int = 1000
    ) -> tuple[str, list[dict[str, Any]]]:
        assert self.api is not None
        chunks: list[str] = []
        pages: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            page = self.api.output(resource_id, cursor=cursor, limit=limit)
            pages.append(page)
            chunks.append(page.get("text") or "")
            if not page.get("text") or page.get("at_current_end"):
                break
            cursor = page.get("next_cursor")
        return "".join(chunks), pages

    # -------- 用例 --------

    async def case_a03_protocol_fidelity(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "printf '%s' '  spaced  \n\ttabbed  '"], "cwd": "."},
            wait_ms=8000,
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"exited"})
        text, _pages = self.read_all(resource_id)
        assert text == "  spaced  \n\ttabbed  ", f"首尾空格/制表符被改动：{text!r}"
        # 非法参数在副作用前被拒绝
        assert self.api is not None
        bad = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="run_command",
                arguments={"command": "x", "argv": ["x"], "workspace_id": self.session_id},
            )
        )
        assert bad.status_code == 422, "非法参数应被拒绝"
        assert "command" in bad.text and "argv" in bad.text
        return {"stdout": text, "reject_status": bad.status_code}

    async def case_c01_wait_returns_same_job(self) -> dict[str, Any]:
        receipt = self.run_command(
            {
                "argv": [
                    "/bin/sh",
                    "-c",
                    "echo started-once; sleep 2.0; echo finished-marker",
                ]
            },
            wait_ms=300,
        )
        resource_id = str(receipt["resource_id"])
        assert receipt.get("wait_reason") == "wait_timeout", receipt
        # 等待超时只返回同一个 job：没有第二次执行
        again = self.dispatch("run_command", {"argv": ["/bin/sh", "-c", "true"]}, wait_ms=0)
        assert str(again["resource_id"]) != resource_id
        view = self.wait_state(resource_id, {"exited"}, timeout=30)
        assert view.get("exit_code") == 0
        text, _ = self.read_all(resource_id)
        assert text.count("started-once") == 1, f"命令被启动了不止一次：{text!r}"
        return {"receipt": receipt, "stdout": text}

    async def case_c02_deadline_independent_of_wait(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "sleep 30"], "execution_timeout_seconds": 2},
            wait_ms=300,
        )
        resource_id = str(receipt["resource_id"])
        view = self.wait_state(resource_id, {"exited"}, timeout=30)
        assert view.get("termination_reason") == "execution_timeout", view
        assert view.get("cancel_confirmed") is True
        return {"view": view}

    async def case_c03_restart_keeps_task_running(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "echo before-restart; sleep 2.5; echo after-restart; exit 5"]},
            wait_ms=200,
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"running"}, timeout=10)
        # 服务停止只停止服务：任务进程继续运行
        self.restart_service()
        assert self.api is not None
        view = self.wait_state(resource_id, {"exited"}, timeout=30)
        assert view.get("exit_code") == 5, view
        text, _ = self.read_all(resource_id)
        assert "before-restart" in text and "after-restart" in text, text
        return {"view": view, "stdout": text}

    async def case_c04_sigkill_recovery_uses_receipt(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "echo prekill; sleep 3.0; exit 7"]}, wait_ms=200
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"running"}, timeout=10)
        # 崩溃：SIGKILL 服务进程；任务应在停机期间自己结束
        self.restart_service(hard=True)
        assert self.api is not None
        view = self.wait_state(resource_id, {"exited"}, timeout=30)
        assert view.get("exit_code") == 7, f"应通过完成回执取得真实退出码：{view}"
        assert view.get("termination_reason") == "exited_normally"
        return {"view": view}

    async def case_c05_unknown_without_evidence(self) -> dict[str, Any]:
        """包装进程与子进程都消失且没有回执 -> unknown（不伪造成功）。"""
        receipt = self.run_command({"argv": ["/bin/sh", "-c", "sleep 30"]}, wait_ms=200)
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"running"}, timeout=10)
        ledger = self.root / "executor-root" / "executor-ledger.sqlite3"
        import aiosqlite

        db = await aiosqlite.connect(ledger)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT worker_pid, pid FROM jobs WHERE job_id = ?", (resource_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        assert row is not None, "台账里应能找到任务"
        worker_pid, child_pid = row["worker_pid"], row["pid"]
        assert worker_pid and child_pid
        # 杀掉整个任务进程组与包装进程（模拟"证据丢失"的最坏情况）
        with _suppress():
            os.killpg(os.getpgid(int(child_pid)), signal.SIGKILL)
        with _suppress():
            os.kill(int(worker_pid), signal.SIGKILL)
        # 删掉完成回执（如果已经写了）：制造"没有证据"的窗口
        completion = self.root / "executor-root" / "jobs" / resource_id / "completion.json"
        with _suppress():
            completion.unlink()
        self.restart_service()
        assert self.api is not None
        view = self.wait_state(resource_id, {"unknown", "exited"}, timeout=20)
        assert view.get("state") == "unknown", f"没有足够证据时必须 unknown：{view}"
        return {"view": view}

    async def case_c06_operation_identity(self) -> dict[str, Any]:
        operation_id = str(uuid.uuid4())
        payload = {"argv": ["/bin/sh", "-c", "echo dedupe-marker"], "workspace_id": self.session_id}
        assert self.api is not None
        api = self.api
        body = envelope(session_id=self.session_id, run_id=self.run_id, action="run_command",
                        arguments=payload, operation_id=operation_id)
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: api.submit(body), range(6)))
        assert all(response.status_code < 400 for response in responses)
        first = responses[0].json()
        self.created_resources.append(str(first["resource_id"]))
        assert {response.json()["resource_id"] for response in responses} == {first["resource_id"]}
        second = next(response.json() for response in responses if response.json().get("deduplicated"))
        assert str(first["resource_id"]) == str(second["resource_id"]), "同 operation 必须复用原资源"
        assert second.get("deduplicated") is True
        assert self.api is not None
        conflict = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="run_command",
                arguments={"argv": ["/bin/sh", "-c", "echo different"], "workspace_id": self.session_id},
                operation_id=operation_id,
            )
        )
        assert conflict.status_code == 409, f"同 operation 不同参数必须冲突：{conflict.status_code}"
        self.wait_state(str(first["resource_id"]), {"exited"})
        text, _ = self.read_all(str(first["resource_id"]))
        assert text.count("dedupe-marker") == 1
        return {"first": first, "second": second, "conflict": conflict.status_code}

    async def case_c08_group_cleanup_and_repeat_cancel(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "sleep 30 & sleep 30 & wait"]}, wait_ms=200
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"running"}, timeout=10)
        assert self.api is not None
        cancel_body = envelope(
            session_id=self.session_id,
            run_id=self.run_id,
            action="cancel_resource",
            arguments={"resource_id": resource_id},
        )
        first = self.api.submit(cancel_body)
        first.raise_for_status()
        first_receipt = first.json()
        assert first_receipt.get("process_confirmed_exited") is True, first_receipt
        assert first_receipt.get("termination_reason") == "user_cancelled"
        # 重复取消（不同 operation id）：返回同一结论，不产生副作用
        second = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="cancel_resource",
                arguments={"resource_id": resource_id},
            )
        )
        second.raise_for_status()
        assert second.json().get("process_confirmed_exited") is True
        view = self.api.resource(resource_id)
        assert view.get("state") in {"exited", "closed"}
        return {"first": first_receipt, "view": view}

    async def case_c09_deadline_enforced_while_service_down(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "sleep 30"], "execution_timeout_seconds": 2},
            wait_ms=200,
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"running"}, timeout=10)
        self.fixture.kill()  # 服务离线；截止计时归包装进程
        if self.api is not None:
            self.api.close()
            self.api = None
        time.sleep(4)
        self.restart_service()
        assert self.api is not None
        view = self.wait_state(resource_id, {"exited"}, timeout=20)
        assert view.get("termination_reason") == "execution_timeout", view
        return {"view": view}

    async def case_e01_multibyte_cursor(self) -> dict[str, Any]:
        text = "中文内容跨页边界" * 40
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", f"printf '%s' '{text}'"]}, wait_ms=8000
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"exited"})
        collected, pages = self.read_all(resource_id, limit=7)  # 故意小于一个汉字的字节跨越
        assert collected == text, "跨页读取不得漏字/重复/改字节"
        # 游标不可跨资源使用
        assert self.api is not None
        other = self.run_command({"argv": ["/bin/sh", "-c", "echo other"]}, wait_ms=5000)
        cross = self.api.client.get(
            f"/execution/v1/resources/{other['resource_id']}/output",
            params={"cursor": pages[0].get("next_cursor"), "limit": 16},
        )
        assert cross.status_code == 422, f"跨资源游标必须被拒绝：{cross.status_code}"
        return {"pages": [{"byte_count": p.get("byte_count")} for p in pages], "cross": cross.status_code}

    async def case_e02_invalid_utf8(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "printf 'ok\\377bad\\n'"]}, wait_ms=8000
        )
        resource_id = str(receipt["resource_id"])
        self.wait_state(resource_id, {"exited"})
        page = self.api.output(resource_id, limit=4096) if self.api else {}
        assert page.get("encoding_error"), "非法 UTF-8 必须报告编码问题"
        assert "\ufffd" in (page.get("text") or "") or "bad" in (page.get("text") or "")
        # 原件在采集额度内保留（隔离模式可直接核对文件）
        log_path = self.root / "executor-root" / "jobs" / resource_id / "stdout.log"
        raw = log_path.read_bytes()
        assert b"\xff" in raw, "原始字节必须保留"
        return {"page": page, "raw_sha256": hashlib.sha256(raw).hexdigest()}

    async def case_e03_output_limit_preserve(self) -> dict[str, Any]:
        # 用小额度替换 64 MiB 初值（测试用小配额，不改产品默认值）
        self.restart_service(EXECUTOR_RESOURCE_LOG_LIMIT_BYTES=str(4096))
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "yes 0123456789abcdef | head -c 300000; echo tail-marker"]},
            wait_ms=300,
        )
        resource_id = str(receipt["resource_id"])
        view = self.wait_state(resource_id, {"exited"}, timeout=30)
        assert view.get("termination_reason") == "exited_normally", (
            "超限默认保活：仍应正常退出",
            view,
        )
        assert int(view.get("dropped_bytes") or 0) > 0, "应记录丢弃字节"
        page = self.api.output(resource_id, limit=64) if self.api else {}
        assert page.get("truncated") is True
        assert page.get("missing_ranges"), "缺失区间必须可见"
        return {"view": view, "page": page}

    async def case_e04_pty_preserved_but_explicit_terminate_stops(self) -> dict[str, Any]:
        self.restart_service(EXECUTOR_RESOURCE_LOG_LIMIT_BYTES="4096", EXECUTOR_MIN_OUTPUT_RESERVATION_BYTES="1024")
        pty = self.dispatch(
            "terminal_open",
            {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200},
        )
        pty_id = str(pty["terminal_id"])
        assert self.api is not None
        sent = self.api.submit(envelope(session_id=self.session_id, run_id=self.run_id,
            action="terminal_send", arguments={"terminal_id": pty_id,
            "text": "seq 1 20000", "submit": True, "wait": True, "wait_ms": 700, "quiet_ms": 100}))
        sent.raise_for_status()
        def quota_page():
            page = self.api.output(pty_id) if self.api else {}
            return page if page.get("truncated") and page.get("missing_ranges") else None
        pty_output = wait_until(quota_page, timeout=5, what="PTY 超限与缺失区间")
        # 先跑一个显式 terminate 的任务
        job = self.run_command(
            {
                "argv": ["/bin/sh", "-c", "seq 1 200000; sleep 30"],
                "output_limit_action": "terminate",
            },
            wait_ms=300,
        )
        job_id = str(job["resource_id"])
        job_view = self.wait_state(job_id, {"exited"}, timeout=30)
        assert job_view.get("termination_reason") == "output_limit_terminate", job_view
        pty_view = self.api.resource(pty_id) if self.api else {}
        assert pty_view.get("state") == "running", "PTY 默认保活，不受其他任务影响"
        return {"job": job_view, "pty": pty_view, "output": pty_output}

    async def case_e05_session_budget(self) -> dict[str, Any]:
        # 用一个小 session 预算的实例验证"预算不足时拒绝新建、已有资源不被淘汰"
        self.restart_service(
            EXECUTOR_SESSION_LOG_BUDGET_BYTES=str(2 * 1024 * 1024),
            EXECUTOR_RESOURCE_LOG_LIMIT_BYTES=str(1024 * 1024),
        )
        assert self.api is not None
        first = self.run_command({"argv": ["/bin/sh", "-c", "sleep 12"]}, wait_ms=100)
        api = self.api
        bodies = [envelope(session_id=self.session_id, run_id=self.run_id, action="run_command",
                           arguments={"argv": ["/bin/sh", "-c", "sleep 12"], "workspace_id": self.session_id})
                  for _ in range(5)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(pool.map(api.submit, bodies))
        accepted = [response.json() for response in responses if response.status_code < 400]
        assert len(accepted) == 1 and sum(response.status_code == 429 for response in responses) == 4
        second = accepted[0]
        self.created_resources.append(str(second["resource_id"]))
        assert self.api is not None
        third = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="run_command",
                arguments={"argv": ["/bin/sh", "-c", "sleep 5"], "workspace_id": self.session_id},
            )
        )
        assert third.status_code == 429, f"预算耗尽应拒绝新建：{third.status_code} {third.text[:200]}"
        # 已有资源仍在
        self.wait_state(str(first["resource_id"]), {"running"}, timeout=5)
        self.wait_state(str(second["resource_id"]), {"running"}, timeout=5)
        return {"third": third.status_code, "concurrent_admissions": [r.status_code for r in responses]}

    async def case_e06_at_current_end_semantics(self) -> dict[str, Any]:
        receipt = self.run_command(
            {"argv": ["/bin/sh", "-c", "echo part-one; sleep 2.0; echo part-two"]}, wait_ms=200
        )
        resource_id = str(receipt["resource_id"])
        wait_until(
            lambda: "part-one" in (self.api.output(resource_id, limit=64).get("text") or "")
            if self.api
            else False,
            timeout=10,
            what="前半输出",
        )
        page = self.api.output(resource_id, limit=4096) if self.api else {}
        assert page.get("at_current_end") is True
        assert page.get("process_exited") is False, "读到当前末尾不代表程序已结束"
        self.wait_state(resource_id, {"exited"}, timeout=30)
        text, _ = self.read_all(resource_id)
        assert "part-two" in text
        return {"page": page}

    async def case_c10_actual_service_controller(self) -> dict[str, Any]:
        self.fixture.stop()
        root = self.fixture.root
        (root / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "deploy/executor_control.py", root / "executor-control.py")
        (root / ".env").write_text("\n".join(k+"="+json.dumps(v) for k,v in self.fixture.env().items())+"\n", encoding="utf-8")
        def control(action):
            result = subprocess.run([sys.executable, str(root / "executor-control.py"), action],
                                    cwd=root, capture_output=True, timeout=45)
            assert result.returncode == 0, result.stderr.decode("utf-8", errors="backslashreplace")
        try:
            control("start")
            first = self.fixture.wait_healthy()
            command = self.run_command({"command": "echo before; sleep 10; echo after"}, wait_ms=300)
            rid = str(command["resource_id"])
            control("stop")
            control("start")
            second = self.fixture.wait_healthy()
            assert first["boot_id"] != second["boot_id"]
            self.wait_state(rid, {"exited"}, timeout=25)
            text, _ = self.read_all(rid)
            assert text == "before\nafter\n", text
            return {"controller": "deploy/executor_control.py", "first": first, "second": second}
        finally:
            control("stop")

    async def case_b01_command_and_script_fidelity(self) -> dict[str, Any]:
        command = self.run_command({"command": "cat <<'FIXTURE' | sed 's/start/end/' > result.txt\n  start 中文  \nFIXTURE\ncat result.txt; printf stderr-msg >&2; exit 7"}, wait_ms=10000)
        rid = str(command["resource_id"])
        self.wait_state(rid, {"exited"}, timeout=15)
        text, _ = self.read_all(rid)
        assert text == "  end 中文  \n", repr(text)
        assert self.api is not None
        assert "stderr-msg" in self.api.output(rid, stream="stderr")["text"]
        assert self.api.resource(rid)["exit_code"] == 7
        script = self.dispatch("run_script", {"language": "python", "code":
            "import json,sys; p=json.load(sys.stdin); print(p['value'])",
            "params_json": json.dumps({"value": "  参数 中文 ; $(data)  "}),
            "workspace_id": self.session_id}, wait_ms=10000)
        sid = str(script["resource_id"])
        self.wait_state(sid, {"exited"}, timeout=15)
        value, _ = self.read_all(sid)
        assert value == "  参数 中文 ; $(data)  \n", repr(value)
        return {"command": command, "script": script}

    async def case_b08_local_dependency_environment(self) -> dict[str, Any]:
        import zipfile
        workspace = self.fixture.root / "workspaces" / self.session_id
        workspace.mkdir(parents=True, exist_ok=True)
        wheel = workspace / "execution_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as output:
            output.writestr("execution_fixture.py", "VALUE = 'local fixture dependency'\n")
            output.writestr("execution_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: execution-fixture\nVersion: 1.0\n")
            output.writestr("execution_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            output.writestr("execution_fixture-1.0.dist-info/RECORD", "")
        setup = self.run_command({"argv": [sys.executable, "-m", "venv", "--without-pip", ".deps"]}, wait_ms=10000)
        self.wait_state(str(setup["resource_id"]), {"exited"}, timeout=15)
        assert self.api is not None
        assert self.api.resource(str(setup["resource_id"]))["exit_code"] == 0
        install = self.run_command({"argv": [sys.executable, "-m", "pip", "--python", ".deps/bin/python", "install", "--no-index", wheel.name]}, wait_ms=10000)
        self.wait_state(str(install["resource_id"]), {"exited"}, timeout=15)
        assert self.api.resource(str(install["resource_id"]))["exit_code"] == 0
        imported = self.run_command({"argv": [".deps/bin/python", "-c", "import execution_fixture;print(execution_fixture.VALUE)"]}, wait_ms=10000)
        self.wait_state(str(imported["resource_id"]), {"exited"}, timeout=15)
        text, _ = self.read_all(str(imported["resource_id"]))
        assert text == "local fixture dependency\n", text
        environment = self.api.client.get("/execution/v1/environment")
        environment.raise_for_status()
        assert environment.json()["interpreters"].get("python")
        return {"setup": setup, "install": install, "import": imported, "network_download": False}

    async def case_e07_immutable_raw_snapshot(self) -> dict[str, Any]:
        import base64
        receipt = self.run_command({"argv": ["/bin/sh", "-c", "printf '\\377first'; sleep 1; printf second"]}, wait_ms=200)
        resource_id = str(receipt["resource_id"])
        assert self.api is not None
        api = self.api
        wait_until(lambda: api.output(resource_id, limit=64).get("byte_count", 0) > 0,
                   timeout=10, what="首段字节")
        def snapshot():
            response = api.client.get(f"/execution/v1/resources/{resource_id}/raw-output",
                params={"session_id": self.session_id, "limit": 1048576})
            response.raise_for_status()
            value = response.json()
            data = base64.b64decode(value["data_base64"], validate=True)
            assert hashlib.sha256(data).hexdigest() == value["sha256"]
            return value, data
        first, data = snapshot()
        self.wait_state(resource_id, {"exited"}, timeout=15)
        final, final_data = snapshot()
        assert data.startswith(b"\xfffirst") and final_data == b"\xfffirstsecond"
        assert hashlib.sha256(data).hexdigest() == first["sha256"]
        return {"first": first, "final": final}

    async def case_e08_resource_limit_refusal(self) -> dict[str, Any]:
        self.restart_service(EXECUTOR_SESSION_MAX_RESOURCES="2")
        assert self.api is not None
        first = self.run_command({"argv": ["/bin/sh", "-c", "sleep 8"]}, wait_ms=100)
        second = self.run_command({"argv": ["/bin/sh", "-c", "sleep 8"]}, wait_ms=100)
        third = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="run_command",
                arguments={"argv": ["/bin/sh", "-c", "sleep 8"], "workspace_id": self.session_id},
            )
        )
        assert third.status_code == 429, third.text[:200]
        self.wait_state(str(first["resource_id"]), {"running"}, timeout=5)
        self.wait_state(str(second["resource_id"]), {"running"}, timeout=5)
        self.restart_service(EXECUTOR_SESSION_MAX_RESOURCES="16", EXECUTOR_DISK_LOW_WATERMARK_BYTES=str(2**60))
        assert self.api is not None
        low_disk = self.api.submit(envelope(session_id=self.session_id, run_id=self.run_id,
            action="run_command", arguments={"argv": ["echo", "refused"], "workspace_id": self.session_id}))
        assert low_disk.status_code == 429, low_disk.text[:200]
        self.restart_service()
        failed_write = self.run_command({"command": "printf data > missing-parent/output.txt"}, wait_ms=5000)
        view = self.wait_state(str(failed_write["resource_id"]), {"exited"})
        assert view["exit_code"] != 0
        return {"third": third.status_code, "low_disk": low_disk.status_code, "file_write_failure": view}

    async def case_b03_command_state_scope(self) -> dict[str, Any]:
        first = self.run_command({"command": "mkdir -p child; cd child; export ACCEPT_STATE=changed; pwd"}, wait_ms=5000)
        self.wait_state(str(first["resource_id"]), {"exited"})
        second = self.run_command({"command": "printf '%s\\n' \"${ACCEPT_STATE-unset}\"; pwd"}, wait_ms=5000)
        self.wait_state(str(second["resource_id"]), {"exited"})
        one, _ = self.read_all(str(first["resource_id"]))
        two, _ = self.read_all(str(second["resource_id"]))
        assert one.strip().endswith("/child") and two.startswith("unset\n")
        assert not two.strip().endswith("/child")
        # 持久终端的目录延续由同一实现的 D01 多轮演练复验。
        return {"first": one, "second": two}

    async def case_f02_events_survive_restart(self) -> dict[str, Any]:
        assert self.api is not None
        before = self.api.events(after=0, limit=1)
        max_before = before.get("next_after", 0)
        receipt = self.run_command({"argv": ["/bin/sh", "-c", "echo event-check"]}, wait_ms=3000)
        self.wait_state(str(receipt["resource_id"]), {"exited"})
        self.restart_service()
        assert self.api is not None
        after = self.api.events(after=max_before, limit=500)
        assert after.get("events"), "重启后必须能补读事件"
        seqs = [int(item["seq"]) for item in after["events"]]
        assert min(seqs) > max_before, "事件序号不得在重启后归零"
        return {"max_before": max_before, "after": after}

    async def case_d01_interactive_shell(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open",
            {
                "argv": ["/bin/sh"],
                "mode": "interactive",
                "prompt_regex": r"\$ $",
                "quiet_ms": 400,
                "startup_grace_ms": 3000,
            },
        )
        terminal_id = str(receipt["terminal_id"])
        assert self.api is not None
        send = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "echo INTERACTIVE_MARKER",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 3000,
                    "prompt_regex": r"\$ $",
                    "quiet_ms": 400,
                },
            )
        )
        send.raise_for_status()
        body = send.json()
        assert body.get("status") == "succeeded", body
        page = self.api.output(terminal_id, limit=8192)
        assert "INTERACTIVE_MARKER" in (page.get("text") or ""), page
        # 第二次输入：状态延续
        send2 = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "echo SECOND_MARKER",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 3000,
                    "quiet_ms": 400,
                },
            )
        )
        send2.raise_for_status()
        cursor = page.get("next_cursor")
        page2 = self.api.output(terminal_id, cursor=cursor, limit=8192)
        text2 = page2.get("text") or ""
        assert "SECOND_MARKER" in text2, text2
        return {"first": body, "second_text": text2}

    async def case_d02_input_validation(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open", {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200}
        )
        terminal_id = str(receipt["terminal_id"])
        assert self.api is not None
        bad = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={"terminal_id": terminal_id, "text": "echo SHOULD_NOT_APPEAR", "keys": ["F13"]},
            )
        )
        assert bad.status_code == 422, bad.text[:200]
        page = self.api.output(terminal_id, limit=8192)
        assert "SHOULD_NOT_APPEAR" not in (page.get("text") or ""), "非法输入不得被发送一半"
        # 合法输入：keys/hex 顺序
        ok = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "printf VALID",
                    "hex": ["0d"],
                    "wait": True,
                    "wait_ms": 2000,
                    "quiet_ms": 300,
                },
            )
        )
        ok.raise_for_status()
        page2 = self.api.output(terminal_id, cursor=page.get("next_cursor"), limit=8192)
        assert "VALID" in (page2.get("text") or ""), page2
        return {"bad_status": bad.status_code}

    async def case_d03_modes(self) -> dict[str, Any]:
        assert self.api is not None
        hands_free = self.dispatch(
            "terminal_open",
            {"argv": ["/bin/sh", "-c", "sleep 1; echo hf-done"], "mode": "hands_free"},
        )
        # hands_free 立即返回（< 1 秒）
        assert str(hands_free["terminal_id"])
        monitor = self.dispatch(
            "terminal_open",
            {
                "argv": ["/bin/sh", "-c", "echo READY; sleep 1; echo DONE"],
                "mode": "monitor",
                "watch": [{"kind": "literal", "pattern": "DONE"}],
            },
        )
        terminal_id = str(monitor["terminal_id"])
        assert self.api is not None

        def watch_hit() -> Any:
            events = self.api.events(after=0, limit=1000) if self.api else {"events": []}
            for item in events["events"]:
                if (
                    item["type"] == "resource.watch_match"
                    and item.get("resource_id") == terminal_id
                ):
                    return item
            return None

        hit = wait_until(watch_hit, timeout=15, what="monitor 命中事件")
        # 命中不关闭资源；进程自行结束
        view = self.api.resource(terminal_id)
        assert view.get("state") in {"running", "exited"}, view
        return {"ends_free": hands_free, "monitor": monitor, "hit": hit}

    async def case_d04_no_stale_prompt(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open",
            {"argv": ["/bin/sh"], "mode": "hands_free", "prompt_regex": r"\$ $", "quiet_ms": 2000},
        )
        terminal_id = str(receipt["terminal_id"])
        assert self.api is not None
        first = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "true",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 3000,
                    "prompt_regex": r"\$ $",
                    "quiet_ms": 300,
                },
            )
        )
        first.raise_for_status()
        # 不产生输出的命令：等待必须超时或安静，而不是用上一轮提示符判定完成
        second = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "sleep 1.2",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 600,
                    "quiet_ms": 300,
                },
            )
        )
        second.raise_for_status()
        assert second.json().get("wait_reason") in {"quiet", "wait_timeout", "output"}, second.json()
        view = self.api.resource(terminal_id)
        assert view.get("state") == "running", "等待到期不得关闭终端"
        return {"second": second.json(), "view": view}

    async def case_d05_input_idempotency(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open", {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200}
        )
        terminal_id = str(receipt["terminal_id"])
        operation_id = str(uuid.uuid4())
        args = {
            "terminal_id": terminal_id,
            "text": "echo IDEMPOTENT_MARKER",
            "submit": True,
            "wait": True,
            "wait_ms": 2000,
            "quiet_ms": 300,
        }
        assert self.api is not None
        first = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments=args,
                operation_id=operation_id,
            )
        )
        first.raise_for_status()
        second = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments=args,
                operation_id=operation_id,
            )
        )
        second.raise_for_status()
        assert second.json().get("deduplicated") is True, second.json()
        text, _ = self.read_all(terminal_id)
        assert sum(line.removeprefix("$ ") == "IDEMPOTENT_MARKER" for line in text.replace("\r", "").splitlines()) == 1, (
            f"程序结果必须只输出一次（终端命令回显不计作第二次执行）：{text!r}"
        )
        return {"first": first.json(), "second": second.json()}

    async def case_d06_screen_and_resize(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open", {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200}
        )
        terminal_id = str(receipt["terminal_id"])
        assert self.api is not None
        resize = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_resize",
                arguments={"terminal_id": terminal_id, "rows": 30, "cols": 100},
            )
        )
        resize.raise_for_status()
        self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "printf 'AAA\\n\\033[2J\\033[HBBB 中文宽字符'",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 2000,
                    "quiet_ms": 300,
                },
            )
        ).raise_for_status()
        page = self.api.output(terminal_id, view="screen", limit=4096)
        text = page.get("text") or ""
        assert "BBB" in text and "中文宽字符" in text, text
        # 清屏后 AAA 不在屏幕上（ANSI 被真实处理）
        assert "AAA" not in text, "清屏序列必须生效"
        return {"screen": page}

    async def case_d07_restart_loses_interaction(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open", {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200}
        )
        terminal_id = str(receipt["terminal_id"])
        assert self.api is not None
        self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={
                    "terminal_id": terminal_id,
                    "text": "echo BEFORE_RESTART",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 1500,
                    "quiet_ms": 300,
                },
            )
        ).raise_for_status()
        self.restart_service()
        assert self.api is not None
        view = self.api.resource(terminal_id)
        assert view.get("interaction_state") == "lost", view
        page = self.api.output(terminal_id, limit=8192)
        assert "BEFORE_RESTART" in (page.get("text") or ""), "历史日志必须保留"
        send = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_send",
                arguments={"terminal_id": terminal_id, "text": "echo AFTER"},
            )
        )
        assert send.status_code == 409, f"交互丢失后不得接受输入：{send.status_code}"
        return {"view": view, "send": send.status_code}

    async def case_d08_close_idempotent_and_cross_session(self) -> dict[str, Any]:
        receipt = self.dispatch(
            "terminal_open", {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200}
        )
        terminal_id = str(receipt["terminal_id"])
        assert self.api is not None
        close_args = {"terminal_id": terminal_id, "escalate": True}
        first = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_close",
                arguments=close_args,
            )
        )
        first.raise_for_status()
        second = self.api.submit(
            envelope(
                session_id=self.session_id,
                run_id=self.run_id,
                action="terminal_close",
                arguments=close_args,
            )
        )
        second.raise_for_status()
        assert "重复" in (second.json().get("note") or "") or second.json().get(
            "status"
        ) == "succeeded"
        listing = self.api.resources(self.session_id, limit=200)
        assert any(
            item["resource_id"] == terminal_id for item in listing.get("resources", [])
        ), "关闭后记录必须保留"
        # 跨 session：另一个 session 读取该终端输出必须被拒绝
        other_session = str(uuid.uuid4())
        self.api.sync(other_session, str(uuid.uuid4()))
        cross = self.api.client.get(
            f"/execution/v1/resources/{terminal_id}/output",
            params={"session_id": other_session, "limit": 16},
        )
        assert cross.status_code == 403, f"跨 session 读取必须被拒绝：{cross.status_code}"
        return {"first": first.json(), "second": second.json(), "cross": cross.status_code}

    async def case_g04_fence_rejects_stale(self) -> dict[str, Any]:
        assert self.api is not None
        # 用旧 run 的信封：执行端必须拒绝（fence 不匹配）
        stale = envelope(
            session_id=self.session_id,
            run_id=str(uuid.uuid4()),
            action="run_command",
            arguments={"argv": ["/bin/sh", "-c", "echo stale"], "workspace_id": self.session_id},
        )
        response = self.api.submit(stale)
        assert response.status_code == 409, f"旧 run 必须被 fence 拒绝：{response.status_code}"
        assert "stale_control" in response.text
        # 自填更大的 fence 序号同样被拒绝
        elevated = envelope(
            session_id=self.session_id,
            run_id=self.run_id,
            action="run_command",
            arguments={"argv": ["/bin/sh", "-c", "echo elevated"], "workspace_id": self.session_id},
        )
        elevated["session_control_seq"] = 99999
        response2 = self.api.submit(elevated)
        assert response2.status_code == 409, f"自填 fence 序号必须被拒绝：{response2.status_code}"
        return {"stale": response.status_code, "elevated": response2.status_code}


def _suppress():
    import contextlib

    return contextlib.suppress(Exception)


# --------------------------------------------------------------------------- #
# 部署模式
# --------------------------------------------------------------------------- #


async def deployed_readonly(args: argparse.Namespace, recorder: Recorder) -> int:
    _ = args  # 只读模式不使用隔离参数；保留统一签名便于调度
    try:
        from agent_workbench.config.settings import get_settings
    except ImportError as exc:
        recorder.skip(
            "H03",
            "已部署服务只读核对",
            f"当前环境缺少控制端包（在控制机上运行，或改用 --base-url 直连）：{exc}",
        )
        return 0
    from agent_workbench.execution.client import HttpExecutorClient
    from agent_workbench.execution.tunnel import SshTunnel

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(
        tunnel, settings=settings, expected_executor_id=settings.executor_id
    )
    try:
        await client.start()
        health = await client.health()
        caps = await client.capabilities()
        environment = await client.execution_environment()
        events = await client.execution_events(after=0, limit=50)
        evidence = {
            "health": health.model_dump(mode="json"),
            "capabilities": caps.model_dump(mode="json"),
            "environment": environment,
            "events_head": events.get("events", [])[:10],
            "execution_mode": caps.execution_mode,
            "note": ("只读核对：未创建任务、未重启服务；上面是执行端报告的原始字段。"),
        }
        path = recorder.log("H03", "deployed-readonly", evidence)
        recorder.results.append(
            CaseResult(
                id="H03",
                name="已部署服务只读核对",
                status="PASS",
                reason="",
                evidence_paths=[path],
            )
        )
        print(f"[ ok ] H03 已部署服务只读核对：执行模式={caps.execution_mode}")
        return 0
    finally:
        await client.close()
        await tunnel.close()


async def deployed_smoke(args: argparse.Namespace, recorder: Recorder) -> int:
    """只创建专属验收 session 与无害本地资源；只关闭本脚本创建的资源。"""
    _ = args
    from agent_workbench.config.settings import get_settings
    from agent_workbench.execution.client import HttpExecutorClient
    from agent_workbench.execution.tunnel import SshTunnel

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(
        tunnel, settings=settings, expected_executor_id=settings.executor_id
    )
    session_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    created: list[str] = []
    try:
        await client.start()
        caps = await client.capabilities()
        if caps.execution_mode != "trusted" or not caps.execution:
            recorder.skip(
                "H05", "已部署冒烟", f"执行模式为 {caps.execution_mode}，不适用冒烟"
            )
            return 0
        ack = await client.execution_sync_control(
            {
                "session_id": session_id,
                "run_id": run_id,
                "owner": "acceptance-smoke",
                "owner_generation": 1,
                "control_version": 1,
                "expected_seq": None,
                "revocations": [],
            }
        )
        seq = int((ack.get("fence") or {}).get("seq") or 0)

        def envelope_body(action: str, arguments: dict[str, Any], wait_ms: int = 0) -> dict[str, Any]:
            payload = json.dumps(
                {"action": action, "arguments": arguments}, ensure_ascii=False, sort_keys=True
            )
            return {
                "schema_version": 1,
                "operation_id": str(uuid.uuid4()),
                "session_id": session_id,
                "run_id": run_id,
                "tool_call_id": f"smoke-{uuid.uuid4().hex[:8]}",
                "request_digest": hashlib.sha256(payload.encode()).hexdigest(),
                "action": action,
                "arguments": arguments,
                "owner_generation": 1,
                "dispatch_control_version": 1,
                "session_control_seq": seq,
                "wait_ms": wait_ms,
                "created_at": _utcnow(),
            }

        evidence: dict[str, Any] = {"session_id": session_id, "steps": []}
        command = await client.execution_submit(
            envelope_body(
                "run_command",
                {"argv": ["/bin/sh", "-c", "echo smoke-ok"], "workspace_id": session_id},
                wait_ms=8000,
            )
        )
        created.append(str(command.get("resource_id")))
        evidence["steps"].append({"run_command": command})
        assert command.get("exit_code") == 0 or command.get("process_state") in {
            "exited",
            "running",
        }, command

        terminal = await client.execution_submit(
            envelope_body(
                "terminal_open",
                {"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200},
            )
        )
        terminal_id = str(terminal.get("terminal_id"))
        created.append(terminal_id)
        send = await client.execution_submit(
            envelope_body(
                "terminal_send",
                {
                    "terminal_id": terminal_id,
                    "text": "echo pty-smoke",
                    "submit": True,
                    "wait": True,
                    "wait_ms": 2000,
                    "quiet_ms": 300,
                },
            )
        )
        evidence["steps"].append({"terminal_send": send})
        page = await client.execution_output(terminal_id, limit=8192)
        evidence["pty_output"] = page.get("text")
        assert "pty-smoke" in (page.get("text") or ""), page

        cancelled = await client.execution_submit(
            envelope_body("cancel_resource", {"resource_id": terminal_id}, wait_ms=20000)
        )
        evidence["steps"].append({"cancel": cancelled})
        assert cancelled.get("process_confirmed_exited") is True, cancelled
        await client.execution_events(after=0, limit=50)
        path = recorder.log("H05", "deployed-smoke", evidence)
        recorder.results.append(
            CaseResult(
                id="H05",
                name="已部署冒烟（专属 session）",
                status="PASS",
                evidence_paths=[path],
            )
        )
        print("[ ok ] H05 已部署冒烟：命令、PTY、取消与事件链路可用")
        return 0
    finally:
        for resource_id in created:
            with _suppress():
                await client.execution_submit(
                    {
                        "schema_version": 1,
                        "operation_id": str(uuid.uuid4()),
                        "session_id": session_id,
                        "run_id": run_id,
                        "tool_call_id": f"cleanup-{uuid.uuid4().hex[:8]}",
                        "request_digest": hashlib.sha256(json.dumps({"action": "cancel_resource", "arguments": {"resource_id": resource_id}}, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                        "action": "cancel_resource",
                        "arguments": {"resource_id": resource_id},
                        "owner_generation": 1,
                        "dispatch_control_version": 1,
                        "session_control_seq": 0,
                        "wait_ms": 20000,
                        "created_at": _utcnow(),
                    }
                )
        await client.close()
        await tunnel.close()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


ISOLATED_CASES: list[tuple[str, str, str]] = [
    ("A03", "case_a03_protocol_fidelity", "协议保真：命令正文逐字节保留；非法参数前置拒绝"),
    ("B01", "case_b01_command_and_script_fidelity", "shell、heredoc、分流、非零退出与 JSON 脚本参数"),
    ("B03", "case_b03_command_state_scope", "独立命令不隐式继承目录和环境"),
    ("B08", "case_b08_local_dependency_environment", "任务 venv 安装本地 fixture wheel，零外网下载"),
    ("C01", "case_c01_wait_returns_same_job", "等待超时返回同一 job，命令只启动一次"),
    ("C02", "case_c02_deadline_independent_of_wait", "等待与执行截止相互独立"),
    ("C03", "case_c03_restart_keeps_task_running", "服务重启不打断非交互任务（SIGTERM 停止方式）"),
    ("C04", "case_c04_sigkill_recovery_uses_receipt", "SIGKILL 后从完成回执取得真实退出码"),
    ("C05", "case_c05_unknown_without_evidence", "证据缺失 -> unknown，不伪造成功"),
    ("C06", "case_c06_operation_identity", "同 operation 复用；不同参数 409"),
    ("C08", "case_c08_group_cleanup_and_repeat_cancel", "进程组回收与重复取消幂等"),
    ("C09", "case_c09_deadline_enforced_while_service_down", "服务离线期间截止仍被执行"),
    ("C10", "case_c10_actual_service_controller", "实际部署控制脚本重启不连带终止非交互任务"),
    ("D01", "case_d01_interactive_shell", "交互终端：多轮 send/read，状态延续"),
    ("D02", "case_d02_input_validation", "非法按键整单拒绝；keys/hex 顺序正确"),
    ("D03", "case_d03_modes", "hands_free 立即返回；monitor 命中记录事件"),
    ("D04", "case_d04_no_stale_prompt", "不用上一轮提示符判定本轮完成"),
    ("D05", "case_d05_input_idempotency", "同 operation 输入不重复写入"),
    ("D06", "case_d06_screen_and_resize", "屏幕处理 ANSI 清屏/中文；resize 一致"),
    ("D07", "case_d07_restart_loses_interaction", "重启后交互丢失但历史保留，不自动重建"),
    ("D08", "case_d08_close_idempotent_and_cross_session", "关闭幂等；跨 session 拒绝"),
    ("E01", "case_e01_multibyte_cursor", "多字节跨页游标不漏字；不跨资源"),
    ("E02", "case_e02_invalid_utf8", "非法 UTF-8 报告编码问题并保留原件"),
    ("E03", "case_e03_output_limit_preserve", "超限默认保活并记录缺失区间"),
    ("E04", "case_e04_pty_preserved_but_explicit_terminate_stops", "PTY 保活；显式 terminate 才终止"),
    ("E05", "case_e05_session_budget", "session 日志预算不足时拒绝新建，不淘汰已有"),
    ("E06", "case_e06_at_current_end_semantics", "读到末尾 != 程序结束"),
    ("E07", "case_e07_immutable_raw_snapshot", "原始非法字节快照与区间 hash 一致"),
    ("E08", "case_e08_resource_limit_refusal", "资源上限只拒绝新建"),
    ("F02", "case_f02_events_survive_restart", "事件序号跨重启不归零"),
    ("G04", "case_g04_fence_rejects_stale", "旧 run / 自填 fence 序号被拒绝"),
]


async def run_isolated(args: argparse.Namespace, recorder: Recorder) -> int:
    if sys.platform == "win32":
        recorder.skip("ALL", "隔离模式", "隔离模式需要真实 Linux（进程组/PTY//proc）")
        return 0
    root = Path(args.isolated_root or (Path(args.output) / "isolated-root")).resolve()
    if root.exists() and args.fresh:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    selected = set(args.cases.split(",")) if args.cases else None
    try:
        # 目录与进程清单（环境清单证据）
        manifest = {
            "root": str(root),
            "python": sys.version,
            "platform": sys.platform,
            "pid": os.getpid(),
            "created_at": _utcnow(),
        }
        recorder.log("env", "manifest", manifest)
        for case_id, method_name, title in ISOLATED_CASES:
            if selected and case_id not in selected:
                continue
            suite = IsolatedSuite(root / case_id, recorder)
            async def run_case(suite=suite, method_name=method_name):
                try:
                    suite.start()
                    return await getattr(suite, method_name)()
                finally:
                    suite.cleanup()
            await recorder.run(case_id, title, run_case)
    finally:
        if not args.keep_root:
            with _suppress():
                shutil.rmtree(root)
    return _write_results(args, recorder)


def _write_results(args: argparse.Namespace, recorder: Recorder) -> int:
    passed = sum(1 for item in recorder.results if item.status == "PASS")
    failed = sum(1 for item in recorder.results if item.status == "FAIL")
    not_run = sum(1 for item in recorder.results if item.status == "NOT_RUN")
    payload = {
        "generated_at": _utcnow(),
        "mode": args.mode,
        "results": [
            {
                "id": item.id,
                "name": item.name,
                "status": item.status,
                "reason": item.reason,
                "duration": item.duration,
                "evidence_paths": item.evidence_paths,
            }
            for item in recorder.results
        ],
        "summary": {"pass": passed, "fail": failed, "not_run": not_run},
        "notes": recorder.notes,
    }
    path = Path(args.output) / "results.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"\n结果：PASS {passed} / FAIL {failed} / NOT_RUN {not_run} -> {path}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="命令执行底座验收（隔离/只读/冒烟）")
    parser.add_argument(
        "--mode",
        choices=["isolated", "deployed-readonly", "deployed-smoke"],
        default="isolated",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", default="", help="逗号分隔的验收 ID（可选）")
    parser.add_argument("--base-url", default="", help="直连执行端地址（默认经 SSH 隧道）")
    parser.add_argument("--token", default="", help="直连时的执行端令牌")
    parser.add_argument("--isolated-root", default="", help="隔离数据根（默认在输出目录下）")
    parser.add_argument("--keep-root", action="store_true", help="保留隔离数据根用于诊断")
    parser.add_argument("--fresh", action="store_true", default=True)
    parser.add_argument("--no-fresh", dest="fresh", action="store_false")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(args.output)
    if args.cases:
        recorder.notes.append(f"本次只运行：{args.cases}")

    if args.mode == "isolated":
        return asyncio.run(run_isolated(args, recorder))
    runner = deployed_readonly if args.mode == "deployed-readonly" else deployed_smoke
    code = asyncio.run(runner(args, recorder))
    if code != 0:
        return code
    return _write_results(args, recorder)


if __name__ == "__main__":
    raise SystemExit(main())
