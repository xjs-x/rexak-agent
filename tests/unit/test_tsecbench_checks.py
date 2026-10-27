"""适配层检查函数测试：运行指纹核对与平台 VPN 状态归因。"""

from __future__ import annotations

from typing import Any, cast

from agent_workbench.agent import factory as factory_module
from agent_workbench.tsecbench import checks
from agent_workbench.tsecbench.config import BenchmarkConfig
from agent_workbench.tsecbench.platform import ChallengeInfo, VpnInfo, VpnUnavailable


def _config() -> BenchmarkConfig:
    return BenchmarkConfig(
        token="token",
        base_url="https://benchmark.example.com",
        api_base_url="http://127.0.0.1:8770",
        api_token="api-token",
    )


def _challenge(code: str, completed: bool) -> ChallengeInfo:
    return ChallengeInfo(
        unique_code=code,
        description="",
        difficulty="easy",
        level="1",
        total_score=100,
        flag_count=1,
        correct_flag_count=1 if completed else 0,
        is_completed=completed,
        container_status="stopped",
        container_addr=(),
    )


async def test_api_runtime_status_flags_version_mismatch(monkeypatch):
    class FakeApi:
        async def health(self) -> dict[str, Any]:
            return {
                "version": "0.1.0",
                "database": {"ok": True},
                "runtime_versions": {"process_start": {"code_version": "old-version"}},
            }

    monkeypatch.setattr(
        factory_module, "loaded_runtime_versions", lambda: {"code_version": "new-version"}
    )
    result = await checks.api_runtime_status(_config(), cast(Any, FakeApi()))

    assert result["status"] == checks.PASS
    assert result["code_matches_source"] is False
    assert result["api_code_version"] == "old-version"
    assert result["local_code_version"] == "new-version"


async def test_platform_status_reports_vpn_failure_without_listing():
    class FakePlatform:
        def __init__(self) -> None:
            self.listed = False

        async def check_vpn(self) -> VpnInfo:
            raise VpnUnavailable("vpn down")

        async def list_challenges(self):
            self.listed = True
            return []

    platform = FakePlatform()
    result = await checks.platform_status(_config(), cast(Any, platform))

    assert result["status"] == checks.FAIL
    assert result["stage"] == "vpn"
    assert platform.listed is False


async def test_platform_status_counts_pending_challenges():
    class FakePlatform:
        async def check_vpn(self) -> VpnInfo:
            return VpnInfo(status="ok", client_ip="10.8.0.2", checked_at="now", ok=True)

        async def list_challenges(self):
            return [_challenge("a", False), _challenge("b", True)]

    result = await checks.platform_status(_config(), cast(Any, FakePlatform()))

    assert result["status"] == checks.PASS
    assert result["challenge_count"] == 2
    assert result["pending_count"] == 1
    assert result["client_ip"] == "10.8.0.2"
