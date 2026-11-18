"""CTFy's instance response must trigger a private, deterministic VPN handoff."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts import kali_docker

from agent_workbench.execution import ctfy_vpn

INSTANCE_ID = "0d9547de-1c33-49ca-87d9-99da38313932"
CONFIG_URL = f"/api/v1/instances/{INSTANCE_ID}/vpn-config"


def test_profile_url_rejects_cross_origin_and_other_paths():
    base = "https://ctfy.tsinghua.edu.cn/api/v1/mcp/"
    assert ctfy_vpn._profile_url(base, CONFIG_URL) == "https://ctfy.tsinghua.edu.cn" + CONFIG_URL
    for value in (
        "https://other.example/api/v1/instances/" + INSTANCE_ID + "/vpn-config",
        "/api/v1/instances/" + INSTANCE_ID + "/attachments/secret",
        CONFIG_URL + "?token=secret",
    ):
        with pytest.raises(ctfy_vpn.CtfyVpnError):
            ctfy_vpn._profile_url(base, value)


async def test_start_instance_fetches_detail_when_surface_url_is_blank(monkeypatch):
    calls: list[str] = []
    profile = b"[Interface]\nPrivateKey = example\n[Peer]\n"

    class Client:
        url = "https://ctfy.tsinghua.edu.cn/api/v1/mcp/"
        token = "never-expose-token"

        async def call_tool(self, name, arguments):
            calls.append(name)
            assert arguments == {"instance_id": INSTANCE_ID}
            return {"structuredContent": {"info": {"vpn_endpoint": {"config_url": CONFIG_URL}}}}

    async def download(_client, url):
        calls.append("download")
        assert url.endswith(CONFIG_URL)
        return profile

    def apply(payload):
        calls.append("apply")
        assert payload == profile
        return True

    monkeypatch.setattr(ctfy_vpn, "_download_profile", download)
    monkeypatch.setattr(ctfy_vpn, "_apply_profile", apply)
    monkeypatch.setattr(ctfy_vpn, "_latest_handshake", lambda: int(time.time()))
    result = await ctfy_vpn.ensure_vpn_for_platform_result(
        Client(),  # type: ignore[arg-type]
        "start_instance",
        {"structuredContent": {"id": INSTANCE_ID, "surface": {"vpn": {"config_url": ""}}}},
        executor_id="kali-docker-01",
    )
    assert result == {"status": "ready", "profile": "changed"}
    assert calls == ["get_instance", "download", "apply"]
    assert "never-expose-token" not in str(result)


def test_runner_imports_deployer_without_repository_root_on_sys_path():
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(root / 'src')!r});"
        "from agent_workbench.execution.ctfy_vpn import _deployer_callable;"
        "assert callable(_deployer_callable())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root / "scripts",
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_active_executor_job_blocks_profile_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(kali_docker, "STATE", tmp_path)
    monkeypatch.setattr(kali_docker, "_active_executor_jobs", lambda: 1)
    with pytest.raises(RuntimeError, match="executing jobs"):
        kali_docker.apply_vpn_bytes(b"[Interface]\nPrivateKey = example\n[Peer]\n")
    assert not (tmp_path / "ctfy" / "wg0.conf").exists()
