"""Executor HTTP 接口（§8.6）。

最小接口集合：

| 方法/路径 | 用途 |
|---|---|
| GET `/health` | 服务版本、启动身份、存储空间、时钟、能力版本 |
| GET `/capabilities` | 浏览器/工具/解释器可用性与版本，避免模型反复探测 |
| POST `/jobs` | 带 operation_id、参数摘要、范围版本接收任务 |
| GET `/jobs/{id}` | 状态、输出游标、工件、最后心跳 |
| GET `/jobs/{id}/output?cursor=...` | 有界增量输出 |
| POST `/jobs/{id}/cancel` | 幂等取消并返回取消状态 |
| GET `/events?after=...` | 重连后补同步远端事件 |
| GET `/artifacts/{id}` | 按权限读取/同步工件 |
| POST `/browser-sessions` | 创建身份隔离会话（P4） |
| POST `/browser-sessions/{id}/restore` | 从状态引用恢复并检查登录有效性（P4） |

**只监听执行岛回环地址**；除 `/health` 外全部要求 Bearer 令牌。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from workbench_executor import __version__
from workbench_executor.ledger import JobRecord, Ledger, OperationConflict
from workbench_executor.policy import (
    NetworkPolicy,
    PolicyDenied,
    ScopeRuleSpec,
    TargetMapping,
)
from workbench_executor.processes import (
    ProcessSupervisor,
    describe_environment,
    minimal_environment,
    read_pid_start_ticks,
)
from workbench_executor.settings import ExecutorSettings, get_executor_settings, new_boot_id

PROTOCOL_VERSION = 1
API_VERSION = "1"

# --------------------------------------------------------------------------- #
# 请求/响应模型
# --------------------------------------------------------------------------- #


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeRulePayload(_Base):
    scheme: str = "any"
    host: str = "*"
    port: int | None = None
    include_subdomains: bool = False
    allow_private: bool = True


class TargetMappingPayload(_Base):
    from_host: str
    from_port: int
    to_host: str
    to_port: int
    host_header: str | None = None
    sni: str | None = None
    scheme: str | None = None


class ScopePayload(_Base):
    revision: int = 1
    rules: list[ScopeRulePayload] = Field(default_factory=list)
    mappings: list[TargetMappingPayload] = Field(default_factory=list)
    denied_metadata_hosts: list[str] = Field(default_factory=list)


class HttpRequestPayload(_Base):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] | None = None
    body: str | None = None
    json_body: Any | None = None
    identity_id: str = "anonymous"
    max_redirects: int = 10
    #: 是否把原始响应字节保存为工件（由控制端决定是否引用）
    save_artifact: bool = True


class CommandPayload(_Base):
    argv: list[str]
    cwd: str | None = None
    timeout_seconds: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    reason: str | None = None


class JobCreateRequest(_Base):
    operation_id: str
    job_id: str | None = None
    kind: Literal["http_request", "command"]
    request_digest: str
    scope_revision: int = 1
    scope: ScopePayload = Field(default_factory=ScopePayload)
    payload: dict[str, Any]
    #: 控制端当前持有的 owner generation；Executor 拒绝已知过期 generation 的新派发
    owner_generation: int = 0
    timeout_seconds: int | None = None


class JobCreateResponse(_Base):
    job_id: str
    operation_id: str
    state: str
    accepted_at: str
    deduplicated: bool


class JobStatusResponse(_Base):
    job_id: str
    operation_id: str
    kind: str
    state: str
    exit_code: int | None = None
    output_cursor: str | None = None
    byte_count: int = 0
    truncated: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    last_heartbeat_at: str | None = None
    cancel_requested: bool = False
    pid: int | None = None
    pid_start_ticks: int | None = None
    worker_boot_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


class OutputResponse(_Base):
    job_id: str
    cursor: str | None = None
    next_cursor: str | None = None
    stream: str = "stdout"
    text: str = ""
    byte_count: int = 0
    eof: bool = False
    truncated: bool = False


class CancelRequest(_Base):
    request_id: str


class CancelResponse(_Base):
    job_id: str
    request_id: str
    state: str
    process_confirmed_exited: bool
    detail: str


class HealthResponse(_Base):
    executor_id: str
    service_version: str
    protocol_version: int
    boot_id: str
    started_at: str
    clock_utc: str
    free_bytes: int
    uptime_seconds: float
    active_jobs: int
    capabilities_version: str


class CapabilitiesResponse(_Base):
    executor_id: str
    service_version: str
    protocol_version: int
    boot_id: str
    started_at: str
    browser_available: bool
    browser_version: str | None = None
    http_available: bool
    command_available: bool
    command_unattended_allowed: bool
    interpreter: str | None = None
    filesystem_available: bool
    network_isolation: str
    capabilities_version: str
    details: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 应用状态
# --------------------------------------------------------------------------- #


class ExecutorState:
    def __init__(self, settings: ExecutorSettings) -> None:
        self.settings = settings
        self.boot_id = new_boot_id()
        self.started_at = datetime.now(UTC)
        self.ledger = Ledger(settings.ledger_path)
        self.supervisor = ProcessSupervisor(
            settings.jobs_root,
            boot_id=self.boot_id,
            output_limit_bytes=settings.job_output_limit_bytes,
            terminate_grace_seconds=settings.command_terminate_grace_seconds,
        )
        self.remote_events: list[dict[str, Any]] = []
        self._event_seq = 0
        self._events_path = settings.data_root / "remote-events.jsonl"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_control_contact: float = time.time()

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._event_seq += 1
        record = {
            "seq": self._event_seq,
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        self.remote_events.append(record)
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def note_control_contact(self) -> None:
        self._last_control_contact = time.time()

    def control_link_age(self) -> float:
        return time.time() - self._last_control_contact


_state: ExecutorState | None = None


def get_state() -> ExecutorState:
    if _state is None:  # pragma: no cover - 由 lifespan 保证
        raise RuntimeError("Executor 状态未初始化")
    return _state


async def _require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """执行端管理接口鉴权。令牌为空则**拒绝启动**而不是放行。"""
    settings = get_executor_settings()
    if not settings.token or settings.token.startswith("change-me"):
        raise HTTPException(
            status_code=503,
            detail="Executor 未配置管理令牌（EXECUTOR_TOKEN），拒绝提供管理接口",
        )
    expected = f"Bearer {settings.token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="管理令牌无效")


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    settings = get_executor_settings()
    if not settings.token or settings.token.startswith("change-me"):
        raise RuntimeError("拒绝启动：未设置 EXECUTOR_TOKEN。无鉴权的执行岛管理接口不允许上线。")
    settings.ensure_directories()
    global _state
    _state = ExecutorState(settings)
    await _state.ledger.open()
    await _reconcile_on_startup(_state)
    _state.emit("executor.started", {"boot_id": _state.boot_id, "version": __version__})
    try:
        yield
    finally:
        await _state.ledger.close()
        _state = None


async def _reconcile_on_startup(state: ExecutorState) -> None:
    """重启核对：原任务可能仍在运行、已退出或无法核实。

    **分别核对，不统一标为失败后自动重跑**（§8.4）。
    """
    for record in await state.ledger.list_unfinished():
        if record.worker_boot_id != state.boot_id:
            # 上一个 boot 的进程：只能靠 PID + start ticks 判断
            alive = False
            if record.pid is not None:
                current_ticks = read_pid_start_ticks(record.pid)
                alive = (
                    record.pid_start_ticks is not None
                    and current_ticks is not None
                    and current_ticks == record.pid_start_ticks
                )
            state.emit(
                "job.reconciled",
                {
                    "job_id": record.job_id,
                    "previous_state": record.state,
                    "process_alive_after_restart": alive,
                    "certainty": "unknown" if record.pid is None else "checked",
                    "note": (
                        "无法核实：缺少 PID 身份信息"
                        if record.pid is None
                        else "已按 PID+start_ticks 核对"
                    ),
                },
            )
            await state.ledger.update(
                record.job_id,
                state="unknown",
                error_code="uncertain_after_restart",
                error_message="Executor 重启后无法核实该任务终态；未自动重跑",
            )


app = FastAPI(
    title="Agent Workbench Executor",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _note_contact(request: Request, call_next):  # type: ignore[no-untyped-def]
    if _state is not None and request.url.path != "/health":
        _state.note_control_contact()
    return await call_next(request)


# --------------------------------------------------------------------------- #
# 健康与能力
# --------------------------------------------------------------------------- #


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    state = get_state()
    usage = shutil.disk_usage(state.settings.data_root)
    return HealthResponse(
        executor_id=state.settings.executor_id,
        service_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        boot_id=state.boot_id,
        started_at=state.started_at.isoformat(),
        clock_utc=datetime.now(UTC).isoformat(),
        free_bytes=usage.free,
        uptime_seconds=(datetime.now(UTC) - state.started_at).total_seconds(),
        active_jobs=await state.ledger.active_count(),
        capabilities_version=state.settings.capabilities_version,
    )


@app.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    dependencies=[Depends(_require_token)],
)
async def capabilities() -> CapabilitiesResponse:
    state = get_state()
    env = describe_environment()
    browser_version = _playwright_version() if state.settings.browser_enabled else None
    return CapabilitiesResponse(
        executor_id=state.settings.executor_id,
        service_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        boot_id=state.boot_id,
        started_at=state.started_at.isoformat(),
        browser_available=browser_version is not None,
        browser_version=browser_version,
        http_available=True,
        command_available=True,
        command_unattended_allowed=state.settings.network_isolation == "enforced",
        interpreter=env["interpreter"],
        filesystem_available=True,
        # 首版如实报告 unavailable：仅靠 Python URL 校验不构成隔离（§10）
        network_isolation=state.settings.network_isolation,
        capabilities_version=state.settings.capabilities_version,
        details={
            "platform": env["platform"],
            "pid_start_ticks_supported": env["pid_start_ticks_supported"],
            "env_allowlist": env["env_allowlist"],
            "control_link_age_seconds": state.control_link_age(),
            "disconnect_grace_seconds": state.settings.disconnect_grace_seconds,
            "notes": [
                f"通用命令模式的无人值守使用要求执行环境级出站约束；当前 network_isolation={state.settings.network_isolation} "
                "因此 command_unattended_allowed=false。",
                "正则命令黑名单、工作目录与提示词均不构成网络隔离。",
            ],
        },
    )


def _playwright_version() -> str | None:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return None
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #


def _build_policy(scope: ScopePayload, settings: ExecutorSettings) -> NetworkPolicy:
    rules = [
        ScopeRuleSpec(
            scheme=item.scheme,
            host=item.host,
            port=item.port,
            include_subdomains=item.include_subdomains,
            allow_private=item.allow_private,
        )
        for item in scope.rules
    ]
    mappings = [
        TargetMapping(
            from_host=item.from_host,
            from_port=item.from_port,
            to_host=item.to_host,
            to_port=item.to_port,
            host_header=item.host_header,
            sni=item.sni,
            scheme=item.scheme,
        )
        for item in scope.mappings
    ]
    return NetworkPolicy(
        rules,
        mappings,
        settings,
        denied_hosts=set(scope.denied_metadata_hosts),
    )


@app.post(
    "/jobs",
    response_model=JobCreateResponse,
    status_code=202,
    dependencies=[Depends(_require_token)],
)
async def create_job(request: JobCreateRequest) -> JobCreateResponse:
    """幂等接收任务。

    同 `operation_id` + 同 `request_digest` 返回原 job；不同 digest 返回 409。
    持久化之后**才**启动进程（§8.5 顺序）。
    """
    state = get_state()

    try:
        existing = await state.ledger.find_by_operation(
            request.operation_id, request.request_digest
        )
    except OperationConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "operation_conflict", "message": str(exc)},
        ) from exc

    if existing is not None:
        return JobCreateResponse(
            job_id=existing.job_id,
            operation_id=existing.operation_id,
            state=existing.state,
            accepted_at=existing.created_at,
            deduplicated=True,
        )

    job_id = request.job_id or str(uuid.uuid4())
    policy = _build_policy(request.scope, state.settings)

    # 先持久化接收事实
    record = await state.ledger.insert(
        job_id=job_id,
        operation_id=request.operation_id,
        request_digest=request.request_digest,
        kind=request.kind,
        payload=request.payload,
        state="received",
    )
    state.emit(
        "job.received",
        {
            "job_id": job_id,
            "operation_id": request.operation_id,
            "kind": request.kind,
            "scope_revision": request.scope_revision,
        },
    )

    task = asyncio.create_task(_run_job(state, record, request, policy), name=f"job-{job_id}")
    state._tasks[job_id] = task

    return JobCreateResponse(
        job_id=job_id,
        operation_id=request.operation_id,
        state="received",
        accepted_at=record.created_at,
        deduplicated=False,
    )


async def _run_job(
    state: ExecutorState,
    record: JobRecord,
    request: JobCreateRequest,
    policy: NetworkPolicy,
) -> None:
    """后台执行任务。异常一律落到台账终态，不留悬挂。"""
    job_id = record.job_id
    try:
        await state.ledger.update(job_id, state="starting")
        if request.kind == "http_request":
            await _run_http_job(state, job_id, request, policy)
        elif request.kind == "command":
            await _run_command_job(state, job_id, request, policy)
        else:  # pragma: no cover - Literal 已约束
            await state.ledger.update(job_id, state="failed", error_code="unsupported_kind")
    except PolicyDenied as exc:
        await state.ledger.update(
            job_id, state="failed", error_code="scope_denied", error_message=str(exc)
        )
        state.emit("job.failed", {"job_id": job_id, "code": "scope_denied", "target": exc.target})
    except asyncio.CancelledError:
        await state.ledger.update(job_id, state="cancelled", error_code="cancelled")
        state.emit("job.cancelled", {"job_id": job_id})
        raise
    except Exception as exc:
        await state.ledger.update(
            job_id,
            state="failed",
            error_code="internal_error",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        state.emit("job.failed", {"job_id": job_id, "code": "internal_error", "message": str(exc)})
    finally:
        state._tasks.pop(job_id, None)


async def _run_http_job(
    state: ExecutorState, job_id: str, request: JobCreateRequest, policy: NetworkPolicy
) -> None:
    from workbench_executor.http_sessions import HttpSessionRegistry

    payload = HttpRequestPayload.model_validate(request.payload)
    registry = HttpSessionRegistry(policy, use_system_proxy=False)
    try:
        started = time.perf_counter()
        response = await registry.request(
            identity_id=payload.identity_id,
            method=payload.method,
            url=payload.url,
            headers=payload.headers,
            params=payload.params,
            content=payload.body,
            json_body=payload.json_body,
            max_redirects=payload.max_redirects,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        job_dir = state.supervisor.job_dir(job_id)
        body_path = job_dir / "response.body"
        body_path.write_bytes(response.body)

        meta = {
            "url": response.url,
            "final_url": response.final_url,
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "http_version": response.http_version,
            "headers": response.headers,
            "content_type": response.content_type,
            "redirect_chain": response.redirect_chain,
            "body_bytes": len(response.body),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
            "truncated": response.truncated,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        (job_dir / "response.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        text, decode_error = response.decoded_text(limit=64 * 1024)
        if decode_error:
            meta["decode_error"] = decode_error
        # 输出日志：只写"可读视图"，原始字节在 response.body 中
        preview = text[:2000]
        (job_dir / "stdout.log").write_text(
            json.dumps(
                {
                    "status_code": response.status_code,
                    "final_url": response.final_url,
                    "content_type": response.content_type,
                    "body_bytes": len(response.body),
                    "elapsed_ms": round(elapsed_ms, 1),
                    "redirect_chain": response.redirect_chain,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n--- 正文前 2000 字符 ---\n"
            + preview,
            encoding="utf-8",
            newline="\n",
        )

        await state.ledger.update(
            job_id,
            state="succeeded",
            exit_code=0,
            byte_count=len(response.body),
            truncated=response.truncated,
            output_path=str(job_dir / "stdout.log"),
            finished_at=datetime.now(UTC).isoformat(),
            error_code=None,
            error_message=None,
        )
        state.emit(
            "job.succeeded",
            {
                "job_id": job_id,
                "status_code": response.status_code,
                "final_url": response.final_url,
                "body_bytes": len(response.body),
            },
        )
    finally:
        await registry.close()


async def _run_command_job(
    state: ExecutorState, job_id: str, request: JobCreateRequest, policy: NetworkPolicy
) -> None:
    payload = CommandPayload.model_validate(request.payload)

    if not policy.generic_command_allowed:
        # 无执行环境级隔离时，通用命令的无人值守模式必须明确关闭（§10）
        await state.ledger.update(
            job_id,
            state="failed",
            error_code="command_unattended_disabled",
            error_message=(
                "执行岛未提供执行环境级出站网络约束（network_isolation="
                f"{state.settings.network_isolation}）；通用命令的无人值守模式已关闭。"
                "正则黑名单/工作目录/提示词都不能替代网络隔离。"
            ),
        )
        state.emit(
            "job.failed",
            {"job_id": job_id, "code": "command_unattended_disabled"},
        )
        return

    cwd = Path(payload.cwd) if payload.cwd else state.settings.workspace_root / job_id
    cwd.mkdir(parents=True, exist_ok=True)

    handle = await state.supervisor.start(
        job_id=job_id,
        argv=payload.argv,
        cwd=cwd,
        env_extra=payload.env,
    )
    await state.ledger.update(
        job_id,
        state="running",
        pid=handle.pid,
        pid_start_ticks=handle.pid_start_ticks,
        worker_boot_id=handle.worker_boot_id,
        started_at=handle.started_at,
        output_path=str(handle.stdout_path),
    )
    state.emit("job.running", {"job_id": job_id, "pid": handle.pid})

    timeout = payload.timeout_seconds or state.settings.command_timeout_seconds
    exit_code, timed_out = await state.supervisor.wait(job_id, timeout_seconds=timeout)
    handle = state.supervisor._handles.get(job_id)

    if timed_out:
        confirmed, detail = await state.supervisor.cancel(job_id)
        await state.ledger.update(
            job_id,
            state="cancelled" if confirmed else "unknown",
            error_code="timeout",
            error_message=f"命令超时（{timeout}s）。{detail}",
            finished_at=datetime.now(UTC).isoformat(),
            byte_count=handle.byte_count if handle else 0,
            truncated=int(handle.truncated) if handle else 0,
        )
        state.emit("job.timeout", {"job_id": job_id, "confirmed_exited": confirmed})
        return

    state_desc = "succeeded" if exit_code == 0 else "failed"
    await state.ledger.update(
        job_id,
        state=state_desc,
        exit_code=exit_code,
        byte_count=handle.byte_count if handle else 0,
        truncated=int(handle.truncated) if handle else 0,
        finished_at=datetime.now(UTC).isoformat(),
    )
    state.emit("job.finished", {"job_id": job_id, "exit_code": exit_code, "state": state_desc})


@app.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(_require_token)],
)
async def get_job(job_id: str) -> JobStatusResponse:
    state = get_state()
    record = await state.ledger.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job 不存在")

    job_dir = state.supervisor.job_dir(job_id)
    artifacts: list[dict[str, Any]] = []
    for name, media in (
        ("response.body", "application/octet-stream"),
        ("stdout.log", "text/plain"),
    ):
        path = job_dir / name
        if path.exists():
            artifacts.append(
                {
                    "name": name,
                    "media_type": media,
                    "byte_size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )

    summary = _summarize(record)
    return JobStatusResponse(
        job_id=record.job_id,
        operation_id=record.operation_id,
        kind=record.kind,
        state=record.state,
        exit_code=record.exit_code,
        output_cursor=_cursor("stdout", record.byte_count),
        byte_count=record.byte_count,
        truncated=record.truncated,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_heartbeat_at=record.last_heartbeat,
        cancel_requested=record.cancel_requested,
        pid=record.pid,
        pid_start_ticks=record.pid_start_ticks,
        worker_boot_id=record.worker_boot_id,
        error_code=record.error_code,
        error_message=record.error_message,
        artifacts=artifacts,
        summary=summary,
    )


def _summarize(record: JobRecord) -> str:
    if record.state == "unknown":
        return "状态无法确认：重启后既不能证明仍在运行，也不能证明已退出。未自动重跑。"
    if record.error_code == "scope_denied":
        return f"被范围策略拒绝：{record.error_message or ''}"
    if record.error_code == "command_unattended_disabled":
        return "通用命令无人值守模式已关闭（缺少执行环境级网络隔离）"
    if record.state == "succeeded":
        return f"执行成功，输出 {record.byte_count} 字节"
    if record.state == "failed":
        return f"执行失败（exit={record.exit_code}）：{record.error_message or ''}"
    return f"当前状态：{record.state}"


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _cursor(stream: str, offset: int) -> str:
    raw = f"v1|{stream}|{offset}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_cursor(cursor: str | None, default_stream: str = "stdout") -> tuple[str, int]:
    if not cursor:
        return default_stream, 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        version, stream, offset = decoded.split("|")
        if version != "v1":
            return default_stream, 0
        return stream, int(offset)
    except (ValueError, UnicodeDecodeError):
        return default_stream, 0


@app.get(
    "/jobs/{job_id}/output",
    response_model=OutputResponse,
    dependencies=[Depends(_require_token)],
)
async def get_job_output(
    job_id: str,
    cursor: str | None = Query(default=None),
    stream: str = Query(default="stdout"),
    limit: int = Query(default=65536, ge=1, le=1_048_576),
) -> OutputResponse:
    """有界增量输出。`cursor` 不透明，客户端不得解析内部结构。"""
    state = get_state()
    record = await state.ledger.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job 不存在")

    resolved_stream, offset = _parse_cursor(cursor, stream)
    data, eof = await state.supervisor.read_output(
        job_id, stream=resolved_stream, offset=offset, limit=limit
    )
    text, decode_error = _decode(data)
    next_offset = offset + len(data)
    return OutputResponse(
        job_id=job_id,
        cursor=cursor,
        next_cursor=_cursor(resolved_stream, next_offset),
        stream=resolved_stream,
        text=text
        if not decode_error
        else f"[该输出块不是合法 UTF-8，原始字节已保留] {decode_error}",
        byte_count=len(data),
        eof=eof and record.state not in {"received", "starting", "running"},
        truncated=record.truncated,
    )


def _decode(data: bytes) -> tuple[str, str | None]:
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return "", f"{exc}"


@app.post(
    "/jobs/{job_id}/cancel",
    response_model=CancelResponse,
    dependencies=[Depends(_require_token)],
)
async def cancel_job(job_id: str, request: CancelRequest) -> CancelResponse:
    """幂等取消。

    - 已取消且已确认退出：返回同一结论，不重复动作；
    - 无法确认退出：返回 `process_confirmed_exited=false`，控制端必须显示 unknown。
    """
    state = get_state()
    record = await state.ledger.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job 不存在")

    if (
        record.state in {"succeeded", "failed", "cancelled"}
        and record.cancel_request_id == request.request_id
    ):
        return CancelResponse(
            job_id=job_id,
            request_id=request.request_id,
            state=record.state,
            process_confirmed_exited=record.state == "cancelled",
            detail="重复的取消请求；返回同一结论",
        )

    confirmed, detail = await state.supervisor.cancel(job_id)
    new_state = (
        record.state
        if confirmed and record.state in {"succeeded", "failed"}
        else ("cancelled" if confirmed else "unknown")
    )
    await state.ledger.update(
        job_id,
        state=new_state,
        cancel_requested=1,
        cancel_request_id=request.request_id,
    )
    state.emit(
        "job.cancel",
        {"job_id": job_id, "request_id": request.request_id, "confirmed": confirmed},
    )
    return CancelResponse(
        job_id=job_id,
        request_id=request.request_id,
        state=new_state,
        process_confirmed_exited=confirmed,
        detail=detail,
    )


@app.get("/events", dependencies=[Depends(_require_token)])
async def read_events(
    after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)
) -> dict[str, Any]:
    """重连后补同步远端事件。"""
    state = get_state()
    events = [item for item in state.remote_events if item["seq"] > after][:limit]
    return {"events": events, "next_after": events[-1]["seq"] if events else after}


@app.get("/artifacts/{job_id}/{name}", dependencies=[Depends(_require_token)])
async def read_artifact(
    job_id: str,
    name: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=1_048_576, ge=1, le=16_777_216),
) -> Response:
    """按权限读取/同步工件，支持续传或有界分块。"""
    state = get_state()
    record = await state.ledger.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job 不存在")
    job_dir = state.supervisor.job_dir(job_id).resolve()
    target = (job_dir / name).resolve()
    if not str(target).startswith(str(job_dir)) or not target.is_file():
        raise HTTPException(status_code=404, detail="工件不存在")
    size = target.stat().st_size
    with target.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(limit)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Artifact-Sha256": _sha256_file(target),
            "X-Artifact-Size": str(size),
            "X-Chunk-Offset": str(offset),
        },
    )


# --------------------------------------------------------------------------- #
# 浏览器会话（P4 交付；此处如实返回未实现，不用假实现冒充）
# --------------------------------------------------------------------------- #


@app.post("/browser-sessions", dependencies=[Depends(_require_token)])
async def create_browser_session() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "code": "not_implemented",
            "message": "浏览器会话在 P4 交付；当前版本不提供该能力，请勿按『已支持』使用。",
        },
    )


@app.post("/browser-sessions/{session_id}/restore", dependencies=[Depends(_require_token)])
async def restore_browser_session(session_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "code": "not_implemented",
            "message": f"browser-sessions/{session_id}/restore 在 P4 交付。",
        },
    )


# --------------------------------------------------------------------------- #
# 错误体统一
# --------------------------------------------------------------------------- #


@app.exception_handler(PolicyDenied)
async def _policy_denied_handler(request: Request, exc: PolicyDenied) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(
        status_code=403,
        content={"code": "scope_denied", "message": exc.reason, "target": exc.target},
    )


@app.exception_handler(OperationConflict)
async def _conflict_handler(request: Request, exc: OperationConflict) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(
        status_code=409, content={"code": "operation_conflict", "message": str(exc)}
    )


def describe_local_environment() -> dict[str, str]:
    """doctor 用：在执行岛本地如实描述环境，不夸大隔离能力。"""
    import sys

    env = minimal_environment()
    return {
        "platform": sys.platform,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "env_keys": ",".join(sorted(env)),
    }
