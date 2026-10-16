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
import contextlib
import hashlib
import json
import os
import shutil
import socket
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from workbench_executor import __version__
from workbench_executor.http_encoding import (
    BODY_MODES,
    BodyConstructionError,
    FormField,
    MultipartPart,
)
from workbench_executor.ledger import JobRecord, Ledger, LedgerMigrationError, OperationConflict
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


class TargetProbeRequest(_Base):
    url: str
    scope: ScopePayload


class HttpRequestPayload(_Base):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] | None = None
    body: str | None = None
    json_body: Any | None = None
    #: 结构化请求体（与 body/json_body 互斥）。边界与编码由执行端统一产生。
    form_fields: list[FormField] | None = None
    multipart_parts: list[MultipartPart] | None = None
    identity_id: str = "anonymous"
    max_redirects: int = 10
    #: 是否把原始响应字节保存为工件（由控制端决定是否引用）
    save_artifact: bool = True
    verify_tls: bool = True
    csrf_source_url: str | None = None
    csrf_input_name: str | None = None
    csrf_destination: Literal["form", "json", "header"] = "form"
    csrf_target_name: str | None = None


class CommandPayload(_Base):
    argv: list[str]
    cwd: str | None = None
    timeout_seconds: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    reason: str | None = None


class FilesystemPayload(_Base):
    """任务工作区文件操作。

    **只接受 `workspace_id`（UUID）而不是路径前缀**：工作区根路径由服务端拼装，
    客户端无法把它指向别处。`path` 只在工作区内解析，越界与软链接逃逸都会被拒。
    """

    action: Literal["list", "read", "write", "edit", "edit_batch", "glob", "search"]
    workspace_id: str
    path: str = ""
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    edits: list[dict[str, Any]] = Field(default_factory=list)
    pattern: str | None = None
    paths: list[str] = Field(default_factory=list)
    regex: bool = False
    case_sensitive: bool = True
    context_lines: int = 0
    cursor: str | None = None
    expected_version: str | None = None
    replace_all: bool = False
    offset: int = 0
    limit: int | None = None


class BrowserPayload(_Base):
    """浏览器动作。

    `identity_id` 决定使用哪个浏览器上下文；不同身份之间的 cookie / storage
    **完全隔离**，切换身份不会影响其他身份。
    """

    action: Literal[
        "navigate",
        "click",
        "fill",
        "press",
        "snapshot",
        "text",
        "screenshot",
        "download",
        "back",
        "login",
    ]
    identity_id: str
    url: str | None = None
    selector: str | None = None
    text: str | None = None
    value: str | None = None
    key: str | None = None
    wait_until: str | None = None
    wait_for_navigation: bool = True
    submit: bool = False
    full_page: bool = False
    max_elements: int | None = None
    max_text_chars: int | None = None
    max_chars: int | None = None
    login_url: str | None = None
    username: str | None = None
    password: str | None = None
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    probe_url: str | None = None
    logged_in_selector: str | None = None


class BrowserSessionRequest(_Base):
    """创建/取回一个身份的浏览器会话。"""

    identity_id: str
    #: 可选探针：给一个 URL 与"已登录才存在"的选择器，用于判定登录态是否仍有效。
    #: 探针是一次真实导航，因此**必须**同时给 scope，否则它无法做范围校验。
    probe_url: str | None = None
    logged_in_selector: str | None = None
    scope: ScopePayload | None = None


class JobCreateRequest(_Base):
    operation_id: str
    job_id: str | None = None
    kind: Literal["http_request", "command", "filesystem", "browser"]
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
    browser_note: str | None = None
    http_available: bool
    command_available: bool
    command_unattended_allowed: bool
    interpreter: str | None = None
    filesystem_available: bool
    network_isolation: str
    capabilities_version: str
    #: 支持的请求体形态（结构化 form/multipart 需要执行端编码器）。
    body_modes: list[str] = Field(default_factory=lambda: list(BODY_MODES))
    details: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 应用状态
# --------------------------------------------------------------------------- #


class ExecutorState:
    def __init__(self, settings: ExecutorSettings) -> None:
        self.settings = settings
        self.boot_id = new_boot_id()
        self.started_at = datetime.now(UTC)
        self._execution_available = False
        self._execution_unavailable_reason = "执行底座未初始化"
        self.ledger = Ledger(settings.ledger_path)
        self.supervisor = ProcessSupervisor(
            settings.jobs_root,
            boot_id=self.boot_id,
            output_limit_bytes=settings.job_output_limit_bytes,
            terminate_grace_seconds=settings.command_terminate_grace_seconds,
        )
        from workbench_executor.browser import BrowserManager
        from workbench_executor.command_runtime import CommandRuntime
        from workbench_executor.http_sessions import HttpSessionRegistry
        from workbench_executor.terminals import TerminalRuntime

        # 浏览器进程常驻：登录态不因一次动作结束而丢失（§10）
        self.browser = BrowserManager(settings)
        self.http_sessions = HttpSessionRegistry(None, use_system_proxy=False)
        self.remote_events: list[dict[str, Any]] = []
        self._event_seq = 0
        self._events_path = settings.data_root / "remote-events.jsonl"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_control_contact: float = time.time()
        # 执行底座（命令/脚本/PTY/监视）。关闭时能力预检如实报告不可用。
        self.command_runtime = CommandRuntime(settings, self.ledger, self.boot_id)
        self.terminals = TerminalRuntime(settings, self.ledger, self.boot_id)
        self.execution_admission_lock = asyncio.Lock()

    @property
    def execution_available(self) -> bool:
        return self._execution_available

    @property
    def execution_unavailable_reason(self) -> str:
        return self._execution_unavailable_reason

    def set_execution_availability(self, available: bool, reason: str = "") -> None:
        self._execution_available = available
        self._execution_unavailable_reason = reason

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
    try:
        await _state.ledger.open()
    except LedgerMigrationError as exc:
        # 历史重复 operation_id 等：明确报告并拒绝启动，不清库、不删记录。
        raise RuntimeError(f"执行端台账迁移失败：{exc}") from exc
    enabled, reason = _execution_availability(_state)
    _state.set_execution_availability(enabled, reason)
    await _reconcile_on_startup(_state)
    await _reconcile_execution_on_startup(_state)
    _state.emit(
        "executor.started",
        {
            "boot_id": _state.boot_id,
            "version": __version__,
            "execution_mode": settings.execution_mode,
            "execution_available": enabled,
        },
    )
    try:
        yield
    finally:
        # 关闭顺序：先把各身份的 storage_state 落盘再关浏览器。
        # 不落盘的话"重启后登录态还在"就是空话。
        with contextlib.suppress(Exception):
            await _state.terminals.shutdown()
        with contextlib.suppress(Exception):
            await _state.browser.close()
        await _state.http_sessions.close()
        await _state.ledger.close()
        _state = None


def _execution_availability(state: ExecutorState) -> tuple[bool, str]:
    """命令可用性预检：配置 + 平台 + 解释器/shell 存在性，不能只看配置文字。"""
    settings = state.settings
    if os.name != "posix":
        return False, "命令执行底座只在 Linux 执行岛提供"
    if settings.execution_mode != "trusted":
        return (
            False,
            "EXECUTOR_EXECUTION_MODE 未设为 trusted；发行缺省保持 disabled。"
            "个人部署可显式设置后重新预检。",
        )
    if not Path(settings.shell_path).exists():
        return False, f"配置的 shell 不存在：{settings.shell_path}"
    interpreter = settings.resolved_python_interpreter
    if not Path(interpreter).exists():
        return False, f"配置的 Python 解释器不存在：{interpreter}"
    return True, ""


async def _reconcile_execution_on_startup(state: ExecutorState) -> None:
    """执行底座恢复核对：包装进程可重管、回执可导入、无证据标 unknown。"""
    try:
        jobs = await state.command_runtime.reconcile()
    except Exception as exc:
        state.emit("execution.reconcile_failed", {"error": f"{type(exc).__name__}: {exc}"})
        jobs = {"error": str(exc)}
    try:
        terminals = await state.terminals.reconcile()
    except Exception as exc:
        state.emit("terminal.reconcile_failed", {"error": f"{type(exc).__name__}: {exc}"})
        terminals = {"error": str(exc)}
    state.emit("execution.reconciled", {"jobs": jobs, "terminals": terminals})


async def _reconcile_on_startup(state: ExecutorState) -> None:
    """重启核对：原任务可能仍在运行、已退出或无法核实。

    **分别核对，不统一标为失败后自动重跑**（§8.4）。
    """
    for record in await state.ledger.list_unfinished():
        if record.is_execution_resource:
            continue
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


# 执行底座子协议（/execution/v1）。写操作集中经 operations；旧接口保持不变。
from workbench_executor.execution_api import create_router as _create_execution_router  # noqa: E402

app.include_router(_create_execution_router(get_state, _require_token))


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
    browser_version = _browser_capability(state.settings)
    execution_details = _execution_capability_details(state)
    return CapabilitiesResponse(
        executor_id=state.settings.executor_id,
        service_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        boot_id=state.boot_id,
        started_at=state.started_at.isoformat(),
        browser_available=browser_version is not None,
        browser_version=browser_version,
        browser_note=(
            "browser_available 依据**是否存在可用可执行文件**判定，不只看 playwright 包是否安装。"
        ),
        http_available=True,
        command_available=execution_details["command"] == "available",
        command_unattended_allowed=False,
        interpreter=env["interpreter"],
        filesystem_available=True,
        # 首版如实报告 unavailable：仅靠 Python URL 校验不构成隔离（§10）
        network_isolation="unavailable",
        capabilities_version=state.settings.capabilities_version,
        body_modes=list(BODY_MODES),
        details={
            "platform": env["platform"],
            "pid_start_ticks_supported": env["pid_start_ticks_supported"],
            "env_allowlist": env["env_allowlist"],
            "control_link_age_seconds": state.control_link_age(),
            "disconnect_grace_seconds": state.settings.disconnect_grace_seconds,
            "execution": execution_details,
            **execution_details,
            "notes": [
                "通用命令模式的无人值守使用要求执行环境级出站约束；当前 network_isolation=unavailable，"
                "因此执行底座以**显式 trusted 配置**为准，且该配置是用户已确认的产品决策："
                "network_isolation 依旧如实报告 unavailable，不因此谎称已隔离。",
                "正则命令黑名单、工作目录与提示词均不构成网络隔离。",
                "body_modes 声明结构化请求体能力；旧控制端不发送结构化体时行为不变。",
                "execution 子协议（/execution/v1）与旧接口并存；旧接口行为不变。",
            ],
        },
    )


def _execution_capability_details(state: ExecutorState) -> dict[str, Any]:
    """执行子协议能力明细。每一项要么 available，要么给出确定原因。"""
    from workbench_executor._generated.contracts import execution_runtime as proto

    settings = state.settings
    available = state.execution_available
    reason = state.execution_unavailable_reason
    pty_ok = state.terminals.available and available
    reasons: dict[str, str] = {}
    if not available:
        reasons["execution"] = reason
    if available and not state.terminals.available:
        reasons["pty"] = "当前平台不支持 PTY（仅 Linux 执行岛提供该能力）"
    return {
        "execution_mode": settings.execution_mode,
        "execution_runtime_versions": [proto.EXECUTION_SUBPROTOCOL_VERSION],
        "command": "available" if available else "unavailable",
        "script": "available" if available else "unavailable",
        "pty": "available" if pty_ok else "unavailable",
        "screen": "available" if pty_ok else "unavailable",
        "monitor": "available" if available else "unavailable",
        "durable_events": "available",
        "detached_job_recovery": "available" if available else "unavailable",
        "output_limit_actions": [item.value for item in proto.OutputLimitAction],
        "workspace_storage_enforcement": settings.workspace_storage_enforcement,
        "dependencies": {"pyte": _module_version("pyte")},
        "unavailable_reasons": reasons,
        "notes": [
            "detached_job_recovery：非交互任务由独立包装进程承载，服务重启不影响其运行。",
            "interactive PTY 在 Executor 重启后会失去交互能力：保留记录、核对残留进程，不自动重建。",
        ],
    }


def _module_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "missing"


@app.post("/target-probe", dependencies=[Depends(_require_token)])
async def target_probe(request: TargetProbeRequest) -> dict[str, Any]:
    """从执行岛检查实际解析、TLS 和 HTTP 连通性；不继承系统代理。"""
    from workbench_executor.http_sessions import HttpSessionRegistry

    state = get_state()
    policy = _build_policy(request.scope, state.settings)
    try:
        mapped, _headers = policy.resolve_mapping(request.url)
        policy.check(mapped)
    except PolicyDenied as exc:
        if "SNI" in str(exc):
            return {"status": "unsupported", "stage": "tls_sni", "reason": str(exc)}
        return {"status": "scope_denied", "stage": "scope", "reason": str(exc)}
    parts = urlsplit(mapped)
    original = urlsplit(request.url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return {"status": "unsupported", "stage": "url", "reason": "只支持 HTTP/HTTPS 目标"}
    if mapped != request.url and original.scheme == "https" and original.hostname != parts.hostname:
        return {
            "status": "unsupported",
            "stage": "tls_sni",
            "reason": "HTTPS 主机映射需要原始主机名的 SNI；当前执行器不能验证该映射",
            "actual_url": mapped,
        }

    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                parts.hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
            ),
            timeout=5,
        )
        resolved = sorted({str(item[4][0]) for item in addresses})
    except Exception as exc:
        return {
            "status": "unreachable",
            "stage": "dns",
            "actual_url": mapped,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    registry = HttpSessionRegistry(policy, use_system_proxy=False)
    try:
        response = await registry.request(
            identity_id="__target_probe__",
            method="HEAD",
            url=request.url,
            max_redirects=5,
            max_body_bytes=0,
        )
    except Exception as exc:
        return {
            "status": "unreachable",
            "stage": "tls_or_http",
            "actual_url": mapped,
            "dns_addresses": resolved,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        await registry.close()
    return {
        "status": "reachable",
        "actual_url": mapped,
        "final_url": response.final_url,
        "dns_addresses": resolved,
        "tls_verified": parts.scheme == "https",
        "http_status": response.status_code,
        "redirect_chain": response.redirect_chain,
        "proxy_inherited": False,
        "note": "HTTP 状态只代表可达性，不代表被测目标安全或存在缺陷。",
    }


def _browser_capability(settings: ExecutorSettings) -> str | None:
    """返回真实可用的浏览器版本，不可用则 None。

    早期实现只要 `import playwright` 成功就报 `browser_available=true`，
    而那只能说明**包**装了 —— 浏览器二进制一条都没有，能力预检等于在说谎。
    这里要求同时满足：包可用 + 存在可执行文件 + 包版本可读。
    """
    if not settings.browser_enabled:
        return None
    executable = settings.resolve_browser_executable()
    if executable is None:
        return None
    try:
        from importlib.metadata import version

        package_version = version("playwright")
    except Exception:
        return None
    return f"{package_version} ({executable})"


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
    ledger_payload = dict(request.payload)
    if request.kind == "browser" and ledger_payload.get("action") == "login":
        # 凭据仅供当前执行任务使用；台账只保存接收事实与脱敏参数。
        # 进程在派发前崩溃时此操作保持 unknown，绝不从脱敏台账重放登录。
        for field in ("username", "password"):
            if field in ledger_payload:
                ledger_payload[field] = "[REDACTED]"
    record = await state.ledger.insert(
        job_id=job_id,
        operation_id=request.operation_id,
        request_digest=request.request_digest,
        kind=request.kind,
        payload=ledger_payload,
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
        elif request.kind == "filesystem":
            await _run_filesystem_job(state, job_id, request)
        elif request.kind == "browser":
            await _run_browser_job(state, job_id, request)
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
    payload = HttpRequestPayload.model_validate(request.payload)
    started = time.perf_counter()
    from workbench_executor.http_sessions import CsrfTokenUnavailable

    try:
        response = await state.http_sessions.request(
            identity_id=payload.identity_id,
            method=payload.method,
            url=payload.url,
            headers=payload.headers,
            params=payload.params,
            content=payload.body,
            json_body=payload.json_body,
            form_fields=[item.model_dump() for item in (payload.form_fields or [])] or None,
            multipart_parts=[item.model_dump() for item in (payload.multipart_parts or [])] or None,
            max_redirects=payload.max_redirects,
            policy=policy,
            csrf_source_url=payload.csrf_source_url,
            csrf_input_name=payload.csrf_input_name,
            csrf_destination=payload.csrf_destination,
            csrf_target_name=payload.csrf_target_name,
            verify_tls=payload.verify_tls,
        )
    except BodyConstructionError as exc:
        # 构造失败 = 请求**没有**发出；这是本地阶段，不是目标响应。
        await state.ledger.update(
            job_id,
            state="failed",
            finished_at=datetime.now(UTC).isoformat(),
            error_code="request_construction_error",
            error_message=str(exc),
        )
        state.emit("job.failed", {"job_id": job_id, "code": "request_construction_error"})
        return
    except CsrfTokenUnavailable as exc:
        await state.ledger.update(
            job_id,
            state="failed",
            finished_at=datetime.now(UTC).isoformat(),
            error_code="csrf_unavailable",
            error_message=str(exc),
        )
        state.emit("job.failed", {"job_id": job_id, "code": "csrf_unavailable"})
        return
    except httpx.HTTPError as exc:
        code = (
            "tls_certificate_error"
            if "CERTIFICATE_VERIFY_FAILED" in str(exc)
            else "http_timeout"
            if isinstance(exc, httpx.TimeoutException)
            else "http_connection_error"
            if isinstance(exc, httpx.ConnectError)
            else "http_transport_error"
        )
        await state.ledger.update(
            job_id,
            state="failed",
            finished_at=datetime.now(UTC).isoformat(),
            error_code=code,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        state.emit("job.failed", {"job_id": job_id, "code": code})
        return
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
        "tls_verified": response.final_url.startswith("https://") and payload.verify_tls,
        "tls_verification_enabled": payload.verify_tls,
        # 请求构造事实：request_encoded 只证明本地构造成立，
        # **不**证明服务端解析成功或测试条件充分。
        "request_encoding": response.request_encoding,
        "request_body_bytes": response.request_body_bytes,
        "request_body_sha256": response.request_body_sha256,
        "request_body_note": (
            "本地构造结果；multipart 的 boundary 随机生成，因此该 hash 不参与逻辑请求去重键。"
        ),
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
                "执行岛未提供已验证的执行环境级出站网络约束（network_isolation="
                f"{policy.network_isolation}）；通用命令的无人值守模式已关闭。"
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


async def _run_filesystem_job(state: ExecutorState, job_id: str, request: JobCreateRequest) -> None:
    """任务工作区文件操作。

    与命令能力**完全不同**：它不启动进程、不联网，只在本任务工作区内读写文件。
    因此它不受"通用命令无人值守模式关闭"的影响 —— 那个限制针对的是任意 argv 与
    出站网络，不是文件访问。

    拦截语义：路径越界/软链接逃逸/版本冲突等都属于**确定错误**（`not_applied`），
    调用方不应重试。
    """
    from workbench_executor.filesystem import WorkspaceError, workspace_for

    payload = FilesystemPayload.model_validate(request.payload)
    job_dir = state.supervisor.job_dir(job_id)

    try:
        workspace = workspace_for(state.settings.workspace_root, payload.workspace_id)
    except WorkspaceError as exc:
        await _write_filesystem_result(state, job_id, job_dir, ok=False, error=exc)
        return

    try:
        if payload.action == "list":
            entries = workspace.list_dir(payload.path)
            result: dict[str, Any] = {
                "action": "list",
                "path": payload.path or ".",
                "entries": [
                    {
                        "path": item.path,
                        "kind": item.kind,
                        "byte_size": item.byte_size,
                        "modified_at": item.modified_at,
                    }
                    for item in entries
                ],
                "count": len(entries),
            }
        elif payload.action == "read":
            content = workspace.read_file(payload.path, offset=payload.offset, limit=payload.limit)
            result = {
                "action": "read",
                "path": content.path,
                "version": content.version,
                "byte_size": content.byte_size,
                "offset": content.offset,
                "truncated": content.truncated,
                "text": content.text,
                "encoding_note": content.encoding_note,
            }
        elif payload.action == "write":
            if payload.content is None:
                raise WorkspaceError("invalid_request", "write 需要提供 content")
            result = {
                "action": "write",
                **workspace.write_file(
                    payload.path,
                    payload.content,
                    expected_version=payload.expected_version,
                ),
            }
        elif payload.action == "edit_batch":
            if not payload.edits:
                raise WorkspaceError("invalid_request", "edit_batch 需要提供 edits")
            if not payload.expected_version:
                raise WorkspaceError(
                    "invalid_request",
                    "edit_batch 必须提供 expected_version（先读后改），避免覆盖并发改动",
                )
            result = {
                "action": "edit_batch",
                **workspace.edit_file_batch(
                    payload.path,
                    edits=payload.edits,
                    expected_version=payload.expected_version,
                ),
            }
        elif payload.action == "glob":
            if not payload.pattern:
                raise WorkspaceError("invalid_request", "glob 需要提供 pattern")
            result = workspace.glob_files(
                payload.pattern,
                root=payload.path or ".",
                cursor=payload.cursor,
                limit=payload.limit or 200,
            )
        elif payload.action == "search":
            if not payload.pattern:
                raise WorkspaceError("invalid_request", "search 需要提供 pattern")
            result = workspace.search_files(
                payload.pattern,
                paths=payload.paths or [payload.path or "."],
                regex=payload.regex,
                case_sensitive=payload.case_sensitive,
                context_lines=payload.context_lines,
                cursor=payload.cursor,
                limit=payload.limit or 200,
            )
        else:  # edit
            if payload.old_text is None or payload.new_text is None:
                raise WorkspaceError("invalid_request", "edit 需要提供 old_text 与 new_text")
            if not payload.expected_version:
                raise WorkspaceError(
                    "invalid_request",
                    "edit 必须提供 expected_version（先读后改），避免覆盖并发改动",
                )
            result = {
                "action": "edit",
                **workspace.edit_file(
                    payload.path,
                    old_text=payload.old_text,
                    new_text=payload.new_text,
                    expected_version=payload.expected_version,
                    replace_all=payload.replace_all,
                ),
            }
    except WorkspaceError as exc:
        await _write_filesystem_result(state, job_id, job_dir, ok=False, error=exc)
        return

    await _write_filesystem_result(state, job_id, job_dir, ok=True, result=result)


async def _write_filesystem_result(
    state: ExecutorState,
    job_id: str,
    job_dir: Any,
    *,
    ok: bool,
    result: dict[str, Any] | None = None,
    error: Any = None,
) -> None:
    """把结果写成 `result.json`，控制端用既有的工件下载接口取回。"""
    payload: dict[str, Any] = {"ok": ok}
    if ok:
        payload["result"] = result or {}
    else:
        payload["error"] = {"code": error.code, "message": error.message}

    (job_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    preview = json.dumps(payload.get("result") or payload.get("error"), ensure_ascii=False)[:2000]
    (job_dir / "stdout.log").write_text(preview + "\n", encoding="utf-8", newline="\n")

    if ok:
        await state.ledger.update(
            job_id,
            state="succeeded",
            exit_code=0,
            output_path=str(job_dir / "stdout.log"),
            finished_at=datetime.now(UTC).isoformat(),
            error_code=None,
            error_message=None,
        )
        state.emit(
            "job.succeeded",
            {"job_id": job_id, "kind": "filesystem", "action": (result or {}).get("action")},
        )
    else:
        code = getattr(error, "code", "workspace_error")
        await state.ledger.update(
            job_id,
            state="failed",
            error_code=code,
            error_message=getattr(error, "message", str(error)),
            finished_at=datetime.now(UTC).isoformat(),
        )
        state.emit("job.failed", {"job_id": job_id, "code": code})


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
        ("result.json", "application/json"),
        ("screenshot.png", "image/png"),
        ("page.txt", "text/plain; charset=utf-8"),
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


@app.get("/artifacts/{job_id}/{name:path}", dependencies=[Depends(_require_token)])
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
    if not target.is_relative_to(job_dir) or not target.is_file():
        raise HTTPException(status_code=404, detail="工件不存在")
    size = target.stat().st_size
    with target.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(limit)
    return Response(
        content=data,
        # 按实际文件类型返回：截图应当是 image/png，正文应当是 text/plain。
        # 一律返回 octet-stream 会让调用方无法凭 HTTP 层判断类型，
        # 也让"下载下来的是不是图"这种基本检查失去意义。
        media_type=_artifact_media_type(target.name),
        headers={
            "X-Artifact-Sha256": _sha256_file(target),
            "X-Artifact-Size": str(size),
            "X-Chunk-Offset": str(offset),
        },
    )


_ARTIFACT_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".csv": "text/csv; charset=utf-8",
}


def _artifact_media_type(name: str) -> str:
    return _ARTIFACT_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------- #
# 浏览器会话（P4 交付；此处如实返回未实现，不用假实现冒充）
# --------------------------------------------------------------------------- #


async def _run_browser_job(state: ExecutorState, job_id: str, request: JobCreateRequest) -> None:
    """浏览器动作。

    范围由**执行岛**在两层强制：导航前判一次（越界连请求都不发），
    以及 `page.route` 对**每个请求**逐跳检查（含 XHR / 图片 / iframe）。
    只检查导航挡不住页面脚本直接 fetch 外部地址。
    """
    from workbench_executor.browser import BrowserActionError, BrowserUnavailable

    payload = BrowserPayload.model_validate(request.payload)
    job_dir = state.supervisor.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    if not state.settings.browser_enabled:
        await _write_browser_result(
            state,
            job_id,
            job_dir,
            ok=False,
            code="browser_disabled",
            message="浏览器能力在配置中被关闭。",
        )
        return

    policy = _build_policy(request.scope, state.settings)
    try:
        result = await state.browser.run_action(
            action=payload.action,
            identity_id=payload.identity_id,
            payload=payload.model_dump(exclude_none=True),
            policy=policy,
            job_dir=job_dir,
        )
    except BrowserUnavailable as exc:
        await _write_browser_result(
            state, job_id, job_dir, ok=False, code="browser_unavailable", message=str(exc)
        )
        return
    except BrowserActionError as exc:
        if payload.action == "login" or exc.code in {"browser_crashed", "page_crashed"}:
            await state.http_sessions.clear(payload.identity_id)
        await _write_browser_result(
            state,
            job_id,
            job_dir,
            ok=False,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )
        return
    except Exception as exc:
        await _write_browser_result(
            state,
            job_id,
            job_dir,
            ok=False,
            code="browser_error",
            message=f"{type(exc).__name__}: {exc}",
        )
        return

    final_url = result.get("final_url")
    if (
        isinstance(final_url, str)
        and final_url.startswith(("http://", "https://"))
        and not result.get("final_url_out_of_scope")
    ):
        try:
            mapped_url = policy.resolve_mapping(final_url)[0]
            policy.check(mapped_url)
            cookies = await state.browser.export_cookies(payload.identity_id, final_url, mapped_url)
            logical_host = (urlsplit(final_url).hostname or "").lower()
            actual_host = (urlsplit(mapped_url).hostname or "").lower()
            if logical_host and actual_host and logical_host != actual_host:
                cookies = [
                    {
                        **cookie,
                        "domain": actual_host
                        if str(cookie.get("domain") or "").lstrip(".").lower() == logical_host
                        else cookie.get("domain"),
                    }
                    for cookie in cookies
                ]
            await state.http_sessions.import_cookies(payload.identity_id, cookies)
        except (PolicyDenied, ValueError):
            # 页面最终位置越界时不导出任何认证状态。
            pass
    await _write_browser_result(state, job_id, job_dir, ok=True, result=result)


async def _write_browser_result(
    state: ExecutorState,
    job_id: str,
    job_dir: Any,
    *,
    ok: bool,
    result: dict[str, Any] | None = None,
    code: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"ok": ok}
    if ok:
        payload["result"] = result or {}
    else:
        payload["error"] = {"code": code, "message": message, "details": details or {}}

    (job_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (job_dir / "stdout.log").write_text(
        json.dumps(payload.get("result") or payload.get("error"), ensure_ascii=False)[:4000] + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if ok:
        await state.ledger.update(
            job_id,
            state="succeeded",
            exit_code=0,
            finished_at=datetime.now(UTC).isoformat(),
            error_code=None,
            error_message=None,
        )
        state.emit(
            "job.succeeded",
            {
                "job_id": job_id,
                "kind": "browser",
                "action": (result or {}).get("action"),
                "final_url_out_of_scope": bool((result or {}).get("final_url_out_of_scope")),
            },
        )
    else:
        await state.ledger.update(
            job_id,
            state="failed",
            error_code=code,
            error_message=message,
            finished_at=datetime.now(UTC).isoformat(),
        )
        state.emit("job.failed", {"job_id": job_id, "kind": "browser", "code": code})


@app.get("/browser-sessions", dependencies=[Depends(_require_token)])
async def list_browser_sessions() -> JSONResponse:
    """列出当前存活的身份会话。"""
    state = get_state()
    return JSONResponse(
        content={
            "browser_available": state.settings.resolve_browser_executable() is not None,
            "browser_alive": state.browser.is_alive(),
            "sessions": state.browser.describe_sessions(),
        }
    )


@app.post("/browser-sessions", dependencies=[Depends(_require_token)])
async def create_browser_session(request: BrowserSessionRequest) -> JSONResponse:
    """创建（或取回）某身份的浏览器会话。

    幂等：同一 `identity_id` 重复调用返回**同一个**上下文，
    因此登录态不会因为重复建会话而丢失。
    """
    from workbench_executor.browser import BrowserActionError, BrowserUnavailable

    state = get_state()
    if not state.settings.browser_enabled:
        return JSONResponse(
            status_code=503,
            content={"code": "browser_disabled", "message": "浏览器能力在配置中被关闭。"},
        )
    try:
        session = await state.browser.session_for(request.identity_id)
    except BrowserActionError as exc:
        return JSONResponse(status_code=422, content={"code": exc.code, "message": exc.message})
    except BrowserUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={"code": "browser_unavailable", "message": str(exc)},
        )

    probe: dict[str, Any] | None = None
    if request.probe_url:
        # 探针仍走范围门：它也是一次真实请求
        if request.scope is None:
            return JSONResponse(
                status_code=422,
                content={
                    "code": "validation_failed",
                    "message": "带 probe_url 时必须同时提供 scope，否则探针无法做范围校验。",
                },
            )
        try:
            policy = _build_policy(request.scope, state.settings)
            probe_result = await state.browser.run_action(
                action="navigate",
                identity_id=request.identity_id,
                payload={
                    "action": "navigate",
                    "identity_id": request.identity_id,
                    "url": request.probe_url,
                },
                policy=policy,
                job_dir=state.supervisor.job_dir("browser-probe"),
            )
            logged_in: bool | None = None
            if request.logged_in_selector:
                try:
                    logged_in = await session.page.locator(request.logged_in_selector).count() > 0
                except Exception:
                    logged_in = None
            probe = {
                "probe_url": request.probe_url,
                "final_url": probe_result.get("final_url"),
                "status": probe_result.get("status"),
                "logged_in_selector": request.logged_in_selector,
                "logged_in": logged_in,
            }
        except BrowserActionError as exc:
            probe = {"probe_url": request.probe_url, "error_code": exc.code, "error": exc.message}

    return JSONResponse(
        content={
            "identity_id": session.identity_id,
            "state_saved": session.state_path.exists(),
            "state_path": str(session.state_path),
            "browser_version": state.browser.browser_version(),
            "probe": probe,
        }
    )


@app.delete("/browser-sessions/{identity_id}", dependencies=[Depends(_require_token)])
async def delete_browser_session(identity_id: str) -> JSONResponse:
    """关闭某身份的会话（先把 storage_state 落盘，登录态可在重建后继续用）。"""
    state = get_state()
    from workbench_executor.browser import BrowserActionError

    try:
        closed = await state.browser.close_session(identity_id)
    except BrowserActionError as exc:
        return JSONResponse(status_code=503, content={"code": exc.code, "message": exc.message})
    return JSONResponse(content={"identity_id": identity_id, "closed": closed})


@app.post("/browser-sessions/{identity_id}/restore", dependencies=[Depends(_require_token)])
async def restore_browser_session(
    identity_id: str, request: BrowserSessionRequest | None = None
) -> JSONResponse:
    """从磁盘上的 storage_state 重建该身份的会话，并可选用探针判定登录是否仍有效。

    **不夸大能力**：这个接口保证的是"把已保存的 cookie/storage 装回去"；
    至于"登录是否真的还有效"，只有在调用方给出 `probe_url` 与
    `logged_in_selector` 时才给出判定，否则如实返回 `logged_in=null`。
    """
    from workbench_executor.browser import BrowserActionError, BrowserUnavailable

    state = get_state()
    try:
        await state.browser.close_session(identity_id)
        session = await state.browser.session_for(identity_id)
    except BrowserActionError as exc:
        return JSONResponse(status_code=422, content={"code": exc.code, "message": exc.message})
    except BrowserUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={"code": "browser_unavailable", "message": str(exc)},
        )

    probe: dict[str, Any] | None = None
    if request is not None and request.probe_url:
        if request.scope is None:
            return JSONResponse(
                status_code=422,
                content={
                    "code": "validation_failed",
                    "message": "带 probe_url 时必须同时提供 scope，否则探针无法做范围校验。",
                },
            )
        try:
            policy = _build_policy(request.scope, state.settings)
            outcome = await state.browser.run_action(
                action="navigate",
                identity_id=identity_id,
                payload={
                    "action": "navigate",
                    "identity_id": identity_id,
                    "url": request.probe_url,
                },
                policy=policy,
                job_dir=state.supervisor.job_dir("browser-probe"),
            )
            logged_in: bool | None = None
            if request.logged_in_selector:
                try:
                    logged_in = await session.page.locator(request.logged_in_selector).count() > 0
                except Exception:
                    logged_in = None
            probe = {
                "probe_url": request.probe_url,
                "final_url": outcome.get("final_url"),
                "status": outcome.get("status"),
                "logged_in_selector": request.logged_in_selector,
                "logged_in": logged_in,
            }
        except BrowserActionError as exc:
            probe = {"probe_url": request.probe_url, "error_code": exc.code, "error": exc.message}

    return JSONResponse(
        content={
            "identity_id": identity_id,
            "restored_from": str(session.state_path),
            "state_existed": session.state_path.exists(),
            "browser_version": state.browser.browser_version(),
            "probe": probe,
        }
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


def _register_execution_error_handlers() -> None:
    """执行子协议的确定错误 -> 结构化 4xx/5xx。控制端据 code 判断，不猜测中文文本。"""
    from workbench_executor.command_runtime import RuntimeRejected
    from workbench_executor.terminals import TerminalRejected

    @app.exception_handler(RuntimeRejected)
    async def _runtime_rejected(request: Request, exc: RuntimeRejected) -> JSONResponse:  # noqa: ARG001
        status_code = {
            "operation_conflict": 409,
            "stale_control": 409,
            "resource_not_found": 404,
            "resource_owner_mismatch": 403,
            "interaction_lost": 409,
            "execution_disabled": 503,
            "dependency_unavailable": 503,
            "resource_limit": 429,
            "invalid_cursor": 422,
        }.get(exc.code, 422)
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(TerminalRejected)
    async def _terminal_rejected(request: Request, exc: TerminalRejected) -> JSONResponse:  # noqa: ARG001
        status_code = {
            "operation_conflict": 409,
            "resource_not_found": 404,
            "resource_owner_mismatch": 403,
            "interaction_lost": 409,
            "dependency_unavailable": 503,
            "resource_limit": 429,
            "invalid_cursor": 422,
        }.get(exc.code, 422)
        return JSONResponse(
            status_code=status_code, content={"code": exc.code, "message": exc.message}
        )


_register_execution_error_handlers()


def describe_local_environment() -> dict[str, str]:
    """doctor 用：在执行岛本地如实描述环境，不夸大隔离能力。"""
    import sys

    env = minimal_environment()
    return {
        "platform": sys.platform,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "env_keys": ",".join(sorted(env)),
    }
