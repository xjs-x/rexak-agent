"""Desktop evaluation orchestration; all solving stays in SingleChallengeFlow."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import asyncssh
import httpx
from dotenv import dotenv_values

from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.contracts.delegation import ExecutionMode
from agent_workbench.platform_compat import child_process_env
from agent_workbench.tsecbench.checks import PASS, api_runtime_status, executor_status
from agent_workbench.tsecbench.config import BenchmarkConfig, private_home
from agent_workbench.tsecbench.flow import FlowOptions, FlowOutcome, SingleChallengeFlow
from agent_workbench.tsecbench.platform import ChallengeInfo, SdkPlatformClient
from agent_workbench.tsecbench.workbench_api import HttpWorkbenchApi

Emit = Callable[[str, object], None]


def last_profile_fields(*, root: Path | None = None) -> dict[str, str]:
    """Restore the last private form; never log its secret values or start work."""
    files = list((root or private_home() / "profiles").glob("*/tsecbench.env"))
    if not files:
        return {}
    values = dotenv_values(max(files, key=lambda path: path.stat().st_mtime), encoding="utf-8")
    return {
        key: value
        for key, value in values.items()
        if value is not None
        and key
        in {
            "BENCHMARK_BASE_URL",
            "BENCHMARK_TOKEN",
            "WORKBENCH_TSECBENCH_OVPN",
            "TSECBENCH_EXECUTION_MODE",
            "TSECBENCH_MAX_SECONDS",
        }
    }


def restrict_path(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        grant = f"{getpass.getuser()}:{'(OI)(CI)' if directory else ''}F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


def validate_inputs(base_url: str, token: str, minutes: str) -> tuple[str, str, float]:
    base_url, token = base_url.strip().rstrip("/"), token.strip()
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("请输入有效的平台根地址，不要在地址中填写密钥、查询参数或片段。")
    if not token or any(char in base_url + token for char in "\r\n\x00"):
        raise ValueError("请输入本轮平台 Token；地址与 Token 必须为单行文本。")
    try:
        seconds = float(minutes) * 60
    except ValueError as exc:
        raise ValueError("单题等待上限必须是分钟数。") from exc
    import math

    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("单题等待上限必须为大于零的有限分钟数。")
    return base_url, token, seconds


def validate_vpn(path: Path) -> bytes:
    if path.stat().st_size > 1_048_576:
        raise ValueError("VPN 配置超过 1 MiB，请检查是否选择了正确文件。")
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    blocked = {
        "up",
        "down",
        "route-up",
        "route-pre-down",
        "ipchange",
        "plugin",
        "config",
        "tls-verify",
        "tls-crypt-v2-verify",
        "auth-user-pass-verify",
        "client-connect",
        "client-disconnect",
        "learn-address",
        "script-security",
        "management",
        "management-client",
    }
    inline = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("</"):
            inline = False
            continue
        if line.startswith("<"):
            inline = line in {
                "<ca>",
                "<cert>",
                "<key>",
                "<tls-auth>",
                "<tls-crypt>",
                "<extra-certs>",
            }
            continue
        if not inline and line.split()[0].strip("\"'").removeprefix("--") in blocked:
            raise ValueError("VPN 文件包含外部配置、脚本或管理指令，请先人工核对该配置。")
    if "remote " not in text or "<key>" not in text:
        raise ValueError("请选择平台下载的、包含远端地址及内嵌私钥的 VPN 配置。")
    return raw


def save_profile(
    base_url: str,
    token: str,
    vpn: Path | None,
    mode: ExecutionMode,
    seconds: float,
    *,
    root: Path | None = None,
) -> BenchmarkConfig:
    """Each save creates a new private profile; previous credentials are preserved."""
    raw = validate_vpn(vpn) if vpn else None
    folder = (root or private_home() / "profiles") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]
    )
    folder.mkdir(parents=True)
    restrict_path(folder, directory=True)
    destination = folder / "client.ovpn" if raw is not None else None
    if destination is not None and raw is not None:
        destination.write_bytes(raw)
        restrict_path(destination)
    env_file = folder / "tsecbench.env"
    values = {"BENCHMARK_BASE_URL": base_url, "BENCHMARK_TOKEN": token}
    values.update({"TSECBENCH_EXECUTION_MODE": mode.value, "TSECBENCH_MAX_SECONDS": str(seconds)})
    if destination:
        values["WORKBENCH_TSECBENCH_OVPN"] = str(destination)
    env_file.write_text(
        "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items()
        ),
        encoding="utf-8",
    )
    restrict_path(env_file)
    cfg = get_settings()
    return BenchmarkConfig(
        base_url=base_url,
        token=token,
        api_token=cfg.access_token,
        api_base_url=f"http://127.0.0.1:{cfg.api_port}",
        execution_mode=mode,
        max_seconds=seconds,
        env_file=str(env_file),
        ovpn_path=str(destination) if destination else None,
    )


def connect_windows_vpn(config: BenchmarkConfig) -> str:
    """Use the installed client's command interface, never place secrets in argv."""
    if os.name != "nt" or not config.ovpn_path:
        raise ValueError("请先选择本轮 VPN 文件；自动连接仅用于 Windows OpenVPN GUI。")
    source = Path(config.ovpn_path)
    raw = validate_vpn(source)
    program = (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "OpenVPN/bin/openvpn-gui.exe"
    )
    if not program.is_file():
        raise RuntimeError("未找到 OpenVPN GUI，请安装官方客户端或自行连接 VPN。")
    folder = Path.home() / "OpenVPN/config"
    folder.mkdir(parents=True, exist_ok=True)
    name = "rexak-eval-" + source.parent.name
    target = folder / f"{name}.ovpn"
    content = (
        raw.decode("utf-8-sig").replace("\r\n", "\n") + "\npull-filter ignore redirect-gateway\n"
    )
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise RuntimeError("同名 VPN 配置内容不同，已保留原文件；请重新选择本轮配置。")
    # Preserve the downloaded original. Only the dedicated client copy is extended.
    target.touch(exist_ok=True)
    restrict_path(target)
    target.write_text(content, encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW
    # rescan needs an existing GUI. A missing GUI is normal on first use;
    # connect starts it automatically, unlike rescan.
    subprocess.run(
        [str(program), "--command", "rescan"],
        check=False,
        capture_output=True,
        timeout=10,
        creationflags=flags,
    )
    subprocess.Popen([str(program), "--command", "connect", name], creationflags=flags)
    return name


def wait_windows_vpn(*, seconds: float = 45) -> None:
    deadline = time.monotonic() + seconds
    with httpx.Client(trust_env=False, timeout=3) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("http://10.0.100.58")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(2)
    raise RuntimeError(
        "已请求客户端连接，但 VPN 健康检查仍未通过。请查看 OpenVPN 状态或权限提示；"
        "若日志为 TLS 超时，检查当前网络是否允许 VPN（此前手机热点可用）。可再次点击连接重试。"
    )


async def kali_vpn_health() -> bool:
    cfg = get_settings()
    script = (
        "import json,urllib.request; "
        "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
        "r=o.open('http://10.0.100.58',timeout=6); "
        "print(json.dumps({'ok':r.status==200 and json.load(r).get('status')=='ok'}))"
    )
    import shlex

    async with asyncssh.connect(
        cfg.executor_host,
        port=cfg.executor_port,
        username=cfg.executor_user,
        client_keys=[str(cfg.executor_ssh_key)],
        known_hosts=str(cfg.executor_known_hosts),
        connect_timeout=8,
    ) as connection:
        result = await connection.run("python3 -c " + shlex.quote(script), check=True, timeout=12)
    return json.loads(str(result.stdout)).get("ok") is True


async def inspect_readiness(
    config: BenchmarkConfig, *, require_api: bool = True
) -> list[ChallengeInfo]:
    config.require_ready()
    async with SdkPlatformClient(config) as platform:
        rows = await platform.list_challenges()
    if not await kali_vpn_health():
        raise RuntimeError("Windows VPN 已通过，但 Kali 健康检查未通过；尚未启动测评。")
    status = await executor_status()
    if status.get("status") != PASS:
        raise RuntimeError("Kali 执行服务检查未通过，请先从原桌面入口检查执行端。")
    if require_api:
        async with HttpWorkbenchApi(config) as api:
            status = await api_runtime_status(config, api)
        if (
            status.get("status") != PASS
            or not status.get("database_ok")
            or not status.get("code_matches_source")
        ):
            raise RuntimeError("本地控制台未就绪或版本不一致，请先启动控制台。")
        async with HttpWorkbenchApi(config) as api:
            models = await api._request("GET", "/api/settings/models")
        default = (models.get("default_provider"), models.get("default_model"))
        if not any(
            (row.get("provider_id"), row.get("model_name")) == default
            and row.get("secret_configured")
            for row in models.get("profiles", [])
        ):
            raise RuntimeError("网页默认模型尚未配置 API Key，请先在网页的模型设置中完成配置。")
        if config.execution_mode is ExecutionMode.ULTRA:
            from scripts.start_workbench import worker_ready

            if not await asyncio.to_thread(worker_ready, get_settings()):
                raise RuntimeError("Ultra Worker 未就绪，请先启动本地控制台。")
    return rows


def console_url(config: BenchmarkConfig, session_id: str | None = None) -> str:
    from uuid import UUID

    return (
        config.api_base_url
        + "/console/"
        + ("?" + urlencode({"session": str(UUID(session_id))}) if session_id else "")
    )


async def evaluate_serial(
    config: BenchmarkConfig,
    codes: Sequence[str],
    stop: threading.Event,
    emit: Emit,
    *,
    flow_factory: Callable[..., SingleChallengeFlow] = SingleChallengeFlow,
    platform_factory: Callable[..., SdkPlatformClient] = SdkPlatformClient,
    api_factory: Callable[..., HttpWorkbenchApi] = HttpWorkbenchApi,
) -> list[FlowOutcome]:
    """Serialize existing single-challenge flows, never run an alternative agent loop."""
    outcomes: list[FlowOutcome] = []
    for index, code in enumerate(dict.fromkeys(codes), 1):
        if stop.is_set():
            break
        async with platform_factory(config) as platform:
            rows = await platform.list_challenges()
        selected = next((row for row in rows if row.unique_code == code), None)
        if selected is None:
            raise RuntimeError(f"本轮题集中不存在 {code}，已停止后续题目。")
        if selected.is_completed:
            emit("log", f"跳过已完成题目：{code}")
            continue
        if any(row.container_status != "stopped" for row in rows):
            raise RuntimeError("平台已有未释放或正在变化的靶场，先处理后再启动测评。")
        emit("log", f"开始第 {index}/{len(codes)} 题：{code}")
        async with api_factory(config) as api:
            flow = flow_factory(
                config=config,
                platform=platform_factory(config),
                api=api,
                options=FlowOptions(unique_code=code, max_seconds=config.max_seconds),
                echo=lambda message: emit("log", message),
                on_run_created=lambda session, run: emit("session", (session, run)),
            )
            outcome = await flow.run()
        outcomes.append(outcome)
        emit("log", f"{code}：{outcome.outcome}，平台进度 {outcome.platform_progress or '待核对'}")
        if not outcome.settled or outcome.outcome in {"failed", "blocked", "cancelled"}:
            emit("log", "当前题存在失败、取消或未决执行，已停止后续题目，请在网页检查。")
            break
        # close_challenge may return before stop_pending settles. Do not overlap targets.
        released = False
        for _ in range(10):
            async with platform_factory(config) as platform:
                rows = await platform.list_challenges()
            if all(row.container_status == "stopped" for row in rows):
                released = True
                break
            await asyncio.sleep(2)
        if not released:
            emit("log", "靶场释放尚未确认，已停止后续题目。")
            break
    return outcomes


class ConsoleProcess:
    """Own only the launcher we create; existing services are left to their owner."""

    def __init__(self) -> None:
        self.child: subprocess.Popen[bytes] | None = None

    async def start(self, config: BenchmarkConfig) -> None:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            try:
                response = await client.get(config.api_base_url + "/health")
                response.raise_for_status()
                if response.json().get("service") != "agent-workbench-api":
                    raise RuntimeError("本地端口被其他程序占用。")
                if not response.json().get("database", {}).get("ok"):
                    raise RuntimeError("现有控制台的数据库未就绪，请检查原启动窗口。")
                if not await self._worker_ready(config):
                    raise RuntimeError("现有控制台缺少 Ultra Worker，请从原启动窗口重新启动。")
                return
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
        if self.child is not None and self.child.poll() is None:
            raise RuntimeError("控制台正在启动，请稍后重试。")
        assert config.env_file is not None
        log = Path(config.env_file).parent / "console-startup.log"
        with log.open("ab") as output:
            self.child = subprocess.Popen(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts/start_workbench.py"),
                    "--api-only",
                    "--no-browser",
                ],
                cwd=REPO_ROOT,
                env=child_process_env(),
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        deadline = time.monotonic() + 150
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                if self.child.poll() is not None:
                    raise RuntimeError(f"控制台启动结束但未就绪，请检查 {log}")
                try:
                    response = await client.get(config.api_base_url + "/health")
                    if (
                        response.status_code == 200
                        and response.json().get("database", {}).get("ok")
                        and (await client.get(config.api_base_url + "/console/")).status_code == 200
                        and await self._worker_ready(config)
                    ):
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError(f"控制台启动尚未完成，请检查 {log}")

    @staticmethod
    async def _worker_ready(config: BenchmarkConfig) -> bool:
        if config.execution_mode is ExecutionMode.STANDARD:
            return True
        from scripts.start_workbench import worker_ready

        return await asyncio.to_thread(worker_ready, get_settings())

    def close(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        child.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        child.wait(timeout=45)


def install_desktop_shortcut() -> Path:
    """A shortcut contains only the public launcher path, never evaluation credentials."""
    if os.name != "nt":
        raise RuntimeError("桌面快捷方式只适用于 Windows。")
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$p=Join-Path $d 'Rexak 评测启动器.lnk';"
        "if(Test-Path -LiteralPath $p){throw '快捷方式已存在，保留原文件'};"
        "$s=$w.CreateShortcut($p);"
        "$s.TargetPath=$env:REXAK_EVALUATION_ENTRY;"
        "$s.WorkingDirectory=$env:REXAK_EVALUATION_ROOT;"
        "$s.Description='Rexak Agent 本地评测启动器';$s.Save();Write-Output $p"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        env=child_process_env(
            {
                "REXAK_EVALUATION_ENTRY": str(REPO_ROOT / "start-evaluation.cmd"),
                "REXAK_EVALUATION_ROOT": str(REPO_ROOT),
            }
        ),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # PowerShell's console encoding is immaterial to the shortcut operation.
    del result
    return Path.home() / "Desktop/Rexak 评测启动器.lnk"
