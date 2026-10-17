"""新成员配置的隔离、重复运行保护及安装失败边界。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import asyncssh
import httpx
import pytest
from dotenv import dotenv_values
from scripts import deploy_executor, setup_workbench
from scripts.deploy_executor import bootstrap_command

from agent_workbench.config.deployment import remote_path, validate_executor_connection
from tests.support.settings import isolated_settings


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "中文 checkout"
    root.mkdir()
    (root / ".env.example").write_bytes((setup_workbench.ROOT / ".env.example").read_bytes())
    return root


def test_independent_config_has_unique_secrets_and_keeps_existing_file(tmp_path):
    root = checkout(tmp_path)
    config = setup_workbench.create_config(root, host="192.0.2.10", user="developer")
    first = config.read_bytes()
    assert not first.startswith(b"\xef\xbb\xbf")
    values = dotenv_values(config)
    assert values["WORKBENCH_EXECUTOR_HOST"] == "192.0.2.10"
    assert values["WORKBENCH_EXECUTOR_USER"] == "developer"
    secrets = [
        values[k]
        for k in ("WORKBENCH_SESSION_SECRET", "WORKBENCH_ACCESS_TOKEN", "WORKBENCH_EXECUTOR_TOKEN")
    ]
    assert len(set(secrets)) == 3 and all(v and not v.startswith("change-me") for v in secrets)
    assert "WORKBENCH_DATA_ROOT" not in values and "WORKBENCH_BACKUP_PATH" not in values
    with pytest.raises(FileExistsError):
        setup_workbench.create_config(root, host="192.0.2.11")
    assert config.read_bytes() == first
    other = tmp_path / "second"
    other.mkdir()
    (other / ".env.example").write_bytes((root / ".env.example").read_bytes())
    second = setup_workbench.create_config(other, host="executor.example")
    assert dotenv_values(second)["WORKBENCH_EXECUTOR_TOKEN"] != values["WORKBENCH_EXECUTOR_TOKEN"]


def test_custom_paths_and_config_injection_are_handled(tmp_path):
    root = checkout(tmp_path)
    key = "C:/Users/Developer Name/中文/id_ed25519"
    config = setup_workbench.create_config(
        root,
        host="kali.local",
        ssh_key=key,
        ssh_port=2222,
        pip_index_url="https://packages.example/simple",
    )
    values = dotenv_values(config)
    assert values["WORKBENCH_EXECUTOR_SSH_KEY"] == key
    assert values["WORKBENCH_EXECUTOR_PORT"] == "2222"
    assert values["WORKBENCH_EXECUTOR_PIP_INDEX_URL"] == "https://packages.example/simple"
    root.joinpath(".env").unlink()
    with pytest.raises(ValueError):
        setup_workbench.create_config(root, host="kali\nXIAOMI_API_KEY=untrusted")
    assert not root.joinpath(".env").exists()
    for index in (
        "https://user:password@packages.example/simple",
        "http://packages.example/simple",
        "https://packages.example/simple?token=fixture",
        "https://packages.example/ bad",
    ):
        with pytest.raises(ValueError, match="HTTPS"):
            setup_workbench.create_config(root, host="kali.local", pip_index_url=index)
    assert not root.joinpath(".env").exists()


def test_remote_paths_resolve_against_ssh_user_and_reject_escapes():
    assert (
        remote_path("~/rexak-agent-data", "/home/developer") == "/home/developer/rexak-agent-data"
    )
    assert remote_path("/srv/my data", "/home/another") == "/srv/my data"
    for value in ("relative", "/", "//", "~/../other", "/srv/../other", "/tmp/bad\npath"):
        with pytest.raises(ValueError):
            remote_path(value, "/home/dev")


def test_missing_kali_fails_before_ssh_and_known_hosts_are_required(tmp_path):
    settings = isolated_settings(executor_host="")
    with pytest.raises(RuntimeError, match="Kali 地址"):
        validate_executor_connection(settings)
    key = tmp_path / "key"
    key.write_text("fixture", encoding="utf-8")
    settings = settings.model_copy(
        update={
            "executor_host": "192.0.2.10",
            "executor_ssh_key": key,
            "executor_known_hosts": tmp_path / "missing",
        }
    )
    with pytest.raises(RuntimeError, match="known_hosts"):
        validate_executor_connection(settings)


def test_install_failure_does_not_deploy_or_start_runner(tmp_path, monkeypatch):
    root = checkout(tmp_path)
    setup_workbench.create_config(root, host="192.0.2.10")
    (root / "executor").mkdir()
    (root / "executor/pyproject.toml").write_text('[project]\nversion="0.1.0"\n', encoding="utf-8")
    settings = isolated_settings(data_root=tmp_path / "data")
    monkeypatch.setattr(setup_workbench, "ROOT", root)
    monkeypatch.setattr(setup_workbench, "get_settings", lambda: settings)
    monkeypatch.setattr(setup_workbench, "validate_executor_connection", lambda _: None)
    monkeypatch.setattr(setup_workbench.shutil, "which", lambda name: name)
    calls = []

    def fail(argv, **kwargs):
        calls.append(argv)
        raise RuntimeError("fixture database failed")

    monkeypatch.setattr(setup_workbench, "run_step", fail)
    with pytest.raises(RuntimeError, match="database failed"):
        setup_workbench.install()
    assert len(calls) == 1
    assert not any("deploy_executor.py" in str(item) or item == "runner" for item in calls[0])


def test_bootstrap_uses_quoted_install_path_and_preserves_partial_venv(tmp_path, monkeypatch):
    command = bootstrap_command("/home/dev name/rexak's executor", bootstrap=True)
    # 在真实临时目录执行远端预检分支，确认失败前没有覆盖半成品环境。
    source = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    venv = tmp_path / ".venv"
    venv.mkdir()
    marker = venv / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["remote-bootstrap", str(tmp_path)])
    with pytest.raises(SystemExit, match="不完整"):
        exec(compile(source, "remote-bootstrap", "exec"), {})
    assert marker.read_text(encoding="utf-8") == "existing"


def test_step_failure_reports_log_without_echoing_child_output(tmp_path, monkeypatch, capsys):
    def failed(*args, **kwargs):
        kwargs["stdout"].write(b"fixture-sensitive-output")
        return subprocess.CompletedProcess(args[0], 7)

    monkeypatch.setattr(setup_workbench.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="退出码 7"):
        setup_workbench.run_step(["fixture"], log=tmp_path / "database.log", cwd=tmp_path)
    assert "fixture-sensitive-output" not in capsys.readouterr().out


async def test_new_executor_without_listener_does_not_probe_ssh_tunnel():
    connection = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(stdout="0\n")),
        forward_local_port=AsyncMock(side_effect=AssertionError("closed remote port")),
    )
    result = await deploy_executor.read_previous_boot_id(
        cast(asyncssh.SSHClientConnection, connection), isolated_settings()
    )
    assert result is None
    connection.forward_local_port.assert_not_called()


@pytest.mark.parametrize("mode", ["healthy", "wrong_identity", "active", "broken_http"])
async def test_listening_executor_is_checked_and_protocol_errors_stop_update(mode, monkeypatch):
    settings = isolated_settings(executor_id="fixture")
    listener = SimpleNamespace(get_port=lambda: 12345, close=Mock())
    connection = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(stdout="1\n")),
        forward_local_port=AsyncMock(return_value=listener),
    )

    def response(_request):
        if mode == "broken_http":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")
        return httpx.Response(
            200,
            json={
                "executor_id": "other" if mode == "wrong_identity" else "fixture",
                "active_jobs": int(mode == "active"),
                "boot_id": "existing-boot",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="http://fixture")
    monkeypatch.setattr(deploy_executor.httpx, "AsyncClient", lambda **_kwargs: client)
    if mode == "healthy":
        assert (
            await deploy_executor.read_previous_boot_id(
                cast(asyncssh.SSHClientConnection, connection), settings
            )
            == "existing-boot"
        )
    else:
        error = httpx.RemoteProtocolError if mode == "broken_http" else RuntimeError
        with pytest.raises(error):
            await deploy_executor.read_previous_boot_id(
                cast(asyncssh.SSHClientConnection, connection), settings
            )
    listener.close.assert_called_once()
