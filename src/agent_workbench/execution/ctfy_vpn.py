"""Deterministic CTFy VPN handoff on the trusted control host.

The model sees only readiness status. Platform credentials and WireGuard
private keys never enter Kali or a model tool argument.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpx

from agent_workbench.execution.ctfy_mcp import CtfyMcpClient, structured_result


class CtfyVpnError(RuntimeError):
    """Safe deployment diagnostic; never contains profile bytes or tokens."""


def _vpn_endpoint(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        for key in ("vpn_endpoint", "vpn"):
            found = value.get(key)
            if isinstance(found, dict) and ("config_url" in found or "host" in found):
                return found
        for child in value.values():
            found = _vpn_endpoint(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _vpn_endpoint(child)
            if found is not None:
                return found
    return None


def _profile_url(base_url: str, config_url: str) -> str:
    base = urlsplit(base_url)
    candidate = urlsplit(urljoin(base_url, config_url))
    if (
        candidate.scheme != "https"
        or candidate.netloc != base.netloc
        or candidate.username is not None
        or candidate.password is not None
        or candidate.query
        or candidate.fragment
        or not re.fullmatch(r"/api/v1/instances/[0-9a-fA-F-]{36}/vpn-config", candidate.path)
    ):
        raise CtfyVpnError("CTFy VPN configuration URL is outside the expected platform endpoint")
    return candidate.geturl()


async def _download_profile(client: CtfyMcpClient, url: str) -> bytes:
    payload = bytearray()
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + client.token},
        timeout=httpx.Timeout(20), follow_redirects=False, trust_env=False,
    ) as transport, transport.stream("GET", url) as response:
        if response.status_code != 200:
            raise CtfyVpnError(f"CTFy VPN configuration download returned HTTP {response.status_code}")
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > 65536:
                raise CtfyVpnError("CTFy VPN configuration exceeds 64 KiB")
    if not payload:
        raise CtfyVpnError("CTFy VPN configuration download was empty")
    return bytes(payload)


def _latest_handshake() -> int:
    result = subprocess.run(
        ["docker", "exec", "workbench-kali", "wg", "show", "wg0", "latest-handshakes"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return max((int(line.split()[-1]) for line in result.stdout.splitlines() if line.split()), default=0)


def _deployer_callable() -> Callable[[bytes], bool]:
    # The Runner starts from scripts/workbench_child.py, so the repository root
    # is not necessarily on sys.path. Load this trusted local script by path.
    script = Path(__file__).resolve().parents[3] / "scripts" / "kali_docker.py"
    spec = importlib.util.spec_from_file_location("rexak_kali_vpn_deploy", script)
    if spec is None or spec.loader is None:
        raise CtfyVpnError("Kali Docker deployment script is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deploy = getattr(module, "apply_vpn_bytes", None)
    if not callable(deploy):
        raise CtfyVpnError("Kali Docker deployment entry point is unavailable")
    return cast(Callable[[bytes], bool], deploy)


def _apply_profile(profile: bytes) -> bool:
    return _deployer_callable()(profile)


async def ensure_vpn_for_platform_result(
    client: CtfyMcpClient, name: str, result: dict[str, Any], *, executor_id: str
) -> dict[str, str] | None:
    """Apply a VPN profile after start/get-instance; never repeat start_instance."""
    if name != "start_instance" or result.get("isError"):
        return None
    body = structured_result(result)
    vpn = _vpn_endpoint(body)
    if vpn is None:
        return None
    if executor_id != "kali-docker-01":
        raise CtfyVpnError("CTFy VPN auto setup requires the Kali Docker Executor")
    config_url = vpn.get("config_url")
    if not isinstance(config_url, str) or not config_url.strip():
        instance_id = body.get("id")
        if not isinstance(instance_id, str):
            raise CtfyVpnError("CTFy start_instance did not return an instance id")
        detail = await client.call_tool("get_instance", {"instance_id": instance_id})
        if detail.get("isError"):
            raise CtfyVpnError("CTFy get_instance could not supply VPN configuration URL")
        vpn = _vpn_endpoint(structured_result(detail))
        config_url = vpn.get("config_url") if vpn is not None else None
        if not isinstance(config_url, str) or not config_url.strip():
            raise CtfyVpnError("CTFy get_instance did not supply VPN configuration URL")
    url = _profile_url(client.url, config_url)
    profile = await _download_profile(client, url)
    deployment = asyncio.create_task(asyncio.to_thread(_apply_profile, profile))
    try:
        changed = await asyncio.shield(deployment)
    except asyncio.CancelledError:
        # Finish the bounded atomic switch/rollback before reporting cancellation.
        await deployment
        raise
    except (ImportError, OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        raise CtfyVpnError(f"CTFy VPN deployment failed ({type(exc).__name__})") from exc
    for _ in range(16):
        try:
            latest = await asyncio.to_thread(_latest_handshake)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise CtfyVpnError(f"CTFy VPN handshake check failed ({type(exc).__name__})") from exc
        if latest and time.time() - latest <= 180:
            return {"status": "ready", "profile": "changed" if changed else "already_installed"}
        await asyncio.sleep(2)
    return {"status": "handshake_pending", "profile": "changed" if changed else "already_installed"}
