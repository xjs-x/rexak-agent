"""配置、平台 VPN 与执行岛连通性检查。

分两层，**不混为一谈**：
1. 控制端（Windows）能经 VPN 到达平台健康检查地址 —— 由 SDK ``check_vpn()`` 证明；
2. 执行岛（Kali）能到达 ``container_addr`` —— 由执行岛的 ``/target-probe`` 证明。

Windows 连上 VPN 不代表 Kali 具备同样路由；任何一层失败都必须明确报告，
不能假定"应该能通"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_workbench.config.settings import get_settings
from agent_workbench.tsecbench.config import BenchmarkConfig
from agent_workbench.tsecbench.platform import PlatformClient, PlatformError, VpnUnavailable
from agent_workbench.tsecbench.workbench_api import HttpWorkbenchApi

PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"


class ProbeError(RuntimeError):
    """执行岛不可达或拒绝探针；不是"目标不可访问"的结论。"""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    url: str
    status: str
    stage: str | None
    detail: str
    http_status: int | None = None

    @property
    def reachable(self) -> bool:
        return self.status == "reachable"


def _reason(payload: dict[str, Any]) -> str:
    for key in ("reason", "error", "detail"):
        value = payload.get(key)
        if value:
            return str(value)[:300]
    return ""


def _as_text(value: Any) -> str:
    """asyncssh 的 stdout 在类型声明里可能是 bytes；显式解码，不做编码猜测。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


async def probe_targets(urls: list[str], scope: dict[str, Any]) -> list[ProbeResult]:
    """从执行岛逐地址探测（HEAD）；一次 SSH 隧道内完成全部探测。"""
    from agent_workbench.execution.client import (
        ExecutorConnectionError,
        ExecutorProtocolError,
        HttpExecutorClient,
    )
    from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(
        tunnel, settings=settings, expected_executor_id=settings.executor_id
    )
    results: list[ProbeResult] = []
    try:
        try:
            await client.start()
        except (TunnelUnavailable, ExecutorConnectionError, ExecutorProtocolError) as exc:
            raise ProbeError(
                f"执行岛不可达（{type(exc).__name__}）：{exc}。"
                "这不是目标不可访问的结论；请先恢复 SSH 隧道与 Executor 进程。"
            ) from exc
        for url in urls:
            try:
                payload = await client.probe_target(url, scope)
            except (ExecutorConnectionError, ExecutorProtocolError) as exc:
                raise ProbeError(
                    f"执行岛探针失败（{url}）：{type(exc).__name__}: {exc}"
                ) from exc
            results.append(
                ProbeResult(
                    url=url,
                    status=str(payload.get("status") or "unknown"),
                    stage=payload.get("stage"),
                    detail=_reason(payload),
                    http_status=payload.get("http_status")
                    if isinstance(payload.get("http_status"), int)
                    else None,
                )
            )
    finally:
        await client.close()
        await tunnel.close()
    return results


async def kali_network_status() -> dict[str, Any]:
    """只读查看 Kali 的 tun 接口与默认路由。

    评测 VPN 只应新增到靶场网段的路由，**不得永久改写默认路由**。
    连接 VPN 前后各看一次默认路由，即可确认没有被劫持。
    """
    import asyncssh

    settings = get_settings()
    try:
        async with asyncssh.connect(
            str(settings.executor_host),
            port=settings.executor_port,
            username=settings.executor_user,
            known_hosts=None,
            client_keys=[str(settings.executor_ssh_key)],
        ) as connection:
            addresses = await connection.run("ip -brief addr show", check=False)
            default_route = await connection.run("ip route show default", check=False)
    except (OSError, asyncssh.Error) as exc:
        return {
            "status": FAIL,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "只读核对失败；不代表 VPN 已修改路由。",
        }
    tun_lines = [
        line.strip()
        for line in _as_text(addresses.stdout).splitlines()
        if line.strip().startswith("tun")
    ]
    return {
        "status": PASS,
        "tun_interfaces": tun_lines,
        "default_route": _as_text(default_route.stdout).strip(),
        "note": (
            "默认路由应与连接评测 VPN 之前一致。若 openvpn 推送了 redirect-gateway，"
            "请用 --pull-filter ignore redirect-gateway 重启，而不是手工改全局路由。"
        ),
    }


async def executor_status() -> dict[str, Any]:
    """执行岛身份与能力（不做目标探测）。"""
    from agent_workbench.execution.client import (
        ExecutorConnectionError,
        ExecutorProtocolError,
        HttpExecutorClient,
    )
    from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(
        tunnel, settings=settings, expected_executor_id=settings.executor_id
    )
    try:
        await client.start()
        health = await client.health()
        capabilities = await client.capabilities()
    except (TunnelUnavailable, ExecutorConnectionError, ExecutorProtocolError) as exc:
        return {
            "status": FAIL,
            "classification": "infrastructure_failure",
            "error": f"{type(exc).__name__}: {exc}",
            "note": "执行岛不可达属基础设施故障，不能据此判断目标不可访问。",
        }
    finally:
        await client.close()
        await tunnel.close()
    return {
        "status": PASS,
        "executor_id": health.executor_id,
        "protocol_version": health.protocol_version,
        "free_bytes": health.free_bytes,
        "execution_mode": capabilities.execution_mode,
        "network_isolation": capabilities.network_isolation,
        "command_available": capabilities.execution_capable("command"),
        "browser_available": capabilities.browser_available,
    }


async def platform_status(config: BenchmarkConfig, platform: PlatformClient) -> dict[str, Any]:
    """平台 VPN 预检 + 题目数量；``platform`` 由调用方持有生命周期。"""
    try:
        vpn = await platform.check_vpn()
    except VpnUnavailable as exc:
        return {"status": FAIL, "stage": "vpn", "error": str(exc)}
    except PlatformError as exc:
        return {"status": FAIL, "stage": "vpn", "error": str(exc)}
    challenges = await platform.list_challenges()
    pending = [item for item in challenges if not item.is_completed]
    return {
        "status": PASS,
        "base_url": config.base_url,
        "vpn_status": vpn.status,
        "client_ip": vpn.client_ip,
        "checked_at": vpn.checked_at,
        "challenge_count": len(challenges),
        "pending_count": len(pending),
    }


async def api_runtime_status(config: BenchmarkConfig, api: HttpWorkbenchApi) -> dict[str, Any]:
    """本机 API 存活 + 加载中的运行指纹 + 历史辅助模式。

    ``process_start.code_version`` 是 API 进程启动时**实际加载的代码指纹**。
    与当前进程重新计算的指纹比较，可以判断"网页/API 是否已经加载了新版"，
    而不是只看源码文件有没有变化。
    """
    from agent_workbench.agent.factory import loaded_runtime_versions

    health = await api.health()
    process = (health.get("runtime_versions") or {}).get("process_start") or {}
    local = loaded_runtime_versions()
    local_version = str(local.get("code_version"))
    loaded_version = str(process.get("code_version") or "")
    settings = get_settings()
    return {
        "status": PASS,
        "api_base_url": config.api_base_url,
        "service_version": health.get("version"),
        "database_ok": (health.get("database") or {}).get("ok"),
        "api_code_version": loaded_version,
        "local_code_version": local_version,
        "code_matches_source": bool(loaded_version) and loaded_version == local_version,
        "history_assistance_mode": settings.history_assistance_mode,
        "decision_memory_mode": settings.decision_memory_mode,
        "note": (
            "code_matches_source=false 表示 API 进程加载的代码与当前源码不同；"
            "需重启 `uv run workbench api` 后再评测。"
        ),
    }


__all__ = [
    "FAIL",
    "NOT_RUN",
    "PASS",
    "ProbeError",
    "ProbeResult",
    "api_runtime_status",
    "executor_status",
    "kali_network_status",
    "platform_status",
    "probe_targets",
]
