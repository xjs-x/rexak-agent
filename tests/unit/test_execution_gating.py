"""能力门（验收 A04）：只在真实能力可用且配置可信时开放执行工具。

- 旧执行端（未声明 `/execution/v1`）-> 确定不可用，**不回退**本机执行；
- `EXECUTOR_EXECUTION_MODE=disabled` -> 确定不可用并给出原因；
- trusted + 逐项 available -> 开放；pty 单独判定；
- 工具选择与逐工具拒绝原因一致（不在列表里 + 调用时确定错误）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_workbench.contracts.tool import ExecutorCapabilities
from agent_workbench.tools.execution_dispatch import ExecutionUnavailable, require_capability
from agent_workbench.tools.registry import build_tools, default_selection
from tests.support.executor_fixtures import fixture_capabilities


def test_legacy_executor_marks_execution_unavailable():
    caps = fixture_capabilities(execution=None)
    with pytest.raises(ExecutionUnavailable) as excinfo:
        require_capability(caps, "command")
    assert excinfo.value.code == "dependency_unavailable"
    selection = default_selection(capabilities=caps)
    assert selection.command is False
    assert selection.terminals is False
    assert selection.environment is False
    names = {getattr(item, "name", "") for item in build_tools(selection)}
    assert "run_command" not in names
    assert "shell_open" not in names
    assert "get_execution_environment" not in names
    # 既有能力不受影响
    assert "http_request" in names
    assert "read_file" in names


def test_disabled_mode_reports_reason_and_hides_tools():
    caps = fixture_capabilities(execution_mode="disabled", command="unavailable")
    with pytest.raises(ExecutionUnavailable) as excinfo:
        require_capability(caps, "command")
    assert excinfo.value.code == "execution_disabled"
    selection = default_selection(capabilities=caps)
    assert selection.command is False
    assert "trusted" in selection.disabled_reasons["command"]
    reasons = selection.disabled_tools()
    assert "run_command" in reasons and "trusted" in reasons["run_command"]


def test_trusted_mode_opens_command_and_pty_independently():
    caps = fixture_capabilities(execution_mode="trusted", command="available", pty="unavailable")
    require_capability(caps, "command")  # 不抛错
    with pytest.raises(ExecutionUnavailable):
        require_capability(caps, "pty")
    selection = default_selection(capabilities=caps)
    assert selection.command is True
    assert selection.terminals is False
    names = {getattr(item, "name", "") for item in build_tools(selection)}
    assert "run_command" in names
    assert "shell_open" not in names


def test_capabilities_without_execution_details_are_never_trusted():
    caps = ExecutorCapabilities(
        executor_id="kali-01",
        service_version="0.1.0",
        protocol_version=1,
        boot_id="boot",
        started_at=datetime(2026, 10, 15, tzinfo=UTC),
        execution_mode="trusted",  # 自报 trusted，但没有明细
        execution={},
    )
    assert caps.execution_capable("command") is False
    with pytest.raises(ExecutionUnavailable):
        require_capability(caps, "command")
