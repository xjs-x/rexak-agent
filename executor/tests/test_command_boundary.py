"""配置文字不能冒充已验证的执行环境级网络隔离。"""

from __future__ import annotations

from workbench_executor.policy import NetworkPolicy
from workbench_executor.settings import ExecutorSettings


def test_unverified_enforced_setting_does_not_unlock_command() -> None:
    settings = ExecutorSettings(token="test-token", network_isolation="enforced")
    policy = NetworkPolicy([], [], settings)

    assert policy.network_isolation == "unavailable"
    assert policy.generic_command_allowed is False
