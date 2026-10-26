"""部署入口的身份、重复启动和上传边界。"""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import httpx
import pytest
from scripts import start_workbench
from scripts.deploy_executor import upload_files

from agent_workbench.config.settings import Settings
from agent_workbench.platform_compat import _bounded_socketpair
from tests.support.settings import isolated_settings


def test_upload_allowlist_excludes_credentials_and_runtime(tmp_path: Path) -> None:
    wheel = tmp_path / "executor.whl"
    wheel.write_bytes(b"wheel")
    files = upload_files(wheel)
    destinations = [relative for _, relative in files]
    assert "executor/src/workbench_executor/api.py" in destinations
    assert "executor-control.py" in destinations
    assert not any(".env" in name or "data/" in name or "tests/" in name for name in destinations)
    assert all(source.is_file() for source, _ in files)


def test_launcher_lock_blocks_second_instance(tmp_path: Path) -> None:
    lock = tmp_path / "launcher.lock"
    with (
        start_workbench.launcher_lock(lock),
        pytest.raises((RuntimeError, OSError)),
        start_workbench.launcher_lock(lock),
    ):
        pytest.fail("second launcher must not acquire the lock")
    with start_workbench.launcher_lock(lock):
        pass


def test_non_loopback_api_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="回环"):
        start_workbench.base_url(Settings(api_host="0.0.0.0"))


def test_complete_launcher_starts_web_and_runner_without_terminal_model_or_key(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "checkout"
    (root / "web/dist").mkdir(parents=True)
    (root / "web/dist/index.html").write_text("fixture", encoding="utf-8")
    cfg = isolated_settings(
        data_root=tmp_path / "data",
        DASHSCOPE_BASE_URL="https://fixture.invalid/compatible-mode/v1",
    )
    monkeypatch.setattr(start_workbench, "ROOT", root)
    monkeypatch.setattr(start_workbench, "get_settings", lambda: cfg)
    monkeypatch.setattr(start_workbench, "port_busy", lambda _: False)
    monkeypatch.setattr(start_workbench, "check_existing_runner", lambda: None)

    async def executor(_settings):
        pass

    checks = []
    spawned = []
    stopped = []

    def check(argv, env, *_args):
        checks.append((list(argv), dict(env)))

    class FakeChild:
        def poll(self):
            return None

    def spawn(argv, **kwargs):
        spawned.append((list(argv), dict(kwargs["env"])))
        if argv[-1] == "runner":
            kwargs["stdout"].write("Runner 已启动\n".encode())
            kwargs["stdout"].flush()
        return FakeChild()

    monkeypatch.setattr(start_workbench, "ensure_executor", executor)
    monkeypatch.setattr(start_workbench, "run_check", check)
    monkeypatch.setattr(start_workbench.subprocess, "Popen", spawn)
    monkeypatch.setattr(start_workbench, "wait_api", lambda *_a: None)
    monkeypatch.setattr(start_workbench, "stop_child", lambda child: stopped.append(child))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("launcher must not prompt"))
    start_workbench.launch(api_only=False, check_only=False, no_browser=True, smoke_seconds=0.01)
    assert len(spawned) == len(stopped) == 2
    api_env, runner_env = spawned[0][1], spawned[1][1]
    assert api_env["XIAOMI_API_KEY"] == api_env["DASHSCOPE_API_KEY"] == ""
    assert runner_env["XIAOMI_API_KEY"] == runner_env["DASHSCOPE_API_KEY"] == ""
    assert all(env["XIAOMI_API_KEY"] == env["DASHSCOPE_API_KEY"] == "" for _, env in checks)
    assert "fixture-selected-key" not in repr([argv for argv, _ in spawned])
    assert "fixture-selected-key" not in capsys.readouterr().out
    assert all(
        "fixture-selected-key" not in p.read_text(encoding="utf-8")
        for p in (cfg.log_root / "launcher").glob("*.log")
    )


def test_ipv6_api_url() -> None:
    assert start_workbench.base_url(Settings(api_host="::1", api_port=8770)) == "http://[::1]:8770"


@pytest.mark.parametrize("status", [401, 403, 503, 302])
def test_executor_preflight_error_identifies_status_without_exposing_secret(status):
    secret = "private-fixture-value"
    response = httpx.Response(
        status,
        text=secret,
        request=httpx.Request(
            "GET",
            "http://fixture.invalid/?token=" + secret,
            headers={"Authorization": "Bearer " + secret},
        ),
    )
    with pytest.raises(RuntimeError) as error:
        start_workbench.check_executor_capabilities(response, "fixture")
    message = str(error.value)
    assert f"HTTP {status}" in message and secret not in message
    if status in {401, 403}:
        assert "WORKBENCH_EXECUTOR_TOKEN" in message


def test_executor_preflight_checks_json_and_identity():
    start_workbench.check_executor_capabilities(
        httpx.Response(200, json={"executor_id": "fixture"}), "fixture"
    )
    for response in (
        httpx.Response(200, json={"executor_id": "other"}),
        httpx.Response(200, json=[]),
        httpx.Response(200, text="invalid"),
    ):
        with pytest.raises(RuntimeError):
            start_workbench.check_executor_capabilities(response, "fixture")


def test_database_upgrade_preserves_all_existing_branch_heads(monkeypatch):
    from alembic import command

    from agent_workbench.cli import _run_alembic_upgrade

    targets = []
    monkeypatch.setattr(command, "upgrade", lambda _cfg, target: targets.append(target))
    _run_alembic_upgrade("unused-no-database-access")
    assert targets == ["heads"]


def executor_control():
    path = Path(__file__).resolve().parents[2] / "deploy/executor_control.py"
    spec = importlib.util.spec_from_file_location("executor_control", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stale_pid_file_does_not_kill_other_process(tmp_path: Path, monkeypatch) -> None:
    control = executor_control()
    pid_file = tmp_path / "executor.pid"
    pid_file.write_text("999999999\n")
    monkeypatch.setattr(control, "PID_FILE", pid_file)
    assert control.owned_pid() is None


def test_existing_executor_is_reused_without_new_process(monkeypatch) -> None:
    control = executor_control()
    monkeypatch.setattr(control, "healthy", lambda: True)
    monkeypatch.setattr(
        control.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("unexpected spawn")
    )
    control.start()


def test_health_rejects_other_executor_identity(monkeypatch) -> None:
    control = executor_control()
    settings = control.get_executor_settings()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"executor_id": settings.executor_id + "-other"})
    )
    client = httpx.Client(transport=transport)
    monkeypatch.setattr(control.httpx, "Client", lambda **_kw: client)
    assert control.healthy() is False


def test_failed_api_start_is_reported_without_waiting() -> None:
    class ExitedChild:
        @staticmethod
        def poll():
            return 1

    with pytest.raises(RuntimeError, match="API 启动失败"):
        start_workbench.wait_api(ExitedChild(), "http://127.0.0.1:8770")  # type: ignore[arg-type]


def test_bounded_socketpair_transmits_and_restores_blocking_mode() -> None:
    reader, writer = _bounded_socketpair()
    try:
        assert reader.gettimeout() is None and writer.gettimeout() is None
        writer.sendall(b"ready")
        assert reader.recv(5) == b"ready"
    finally:
        reader.close()
        writer.close()


def test_socketpair_timeout_closes_created_sockets(monkeypatch) -> None:
    from agent_workbench import platform_compat

    class FakeSocket:
        closed = False
        timeout = None

        def settimeout(self, value):
            self.timeout = value

        def bind(self, address):
            pass

        def listen(self, backlog):
            pass

        def getsockname(self):
            return ("127.0.0.1", 50000)

        def connect(self, address):
            pass

        def accept(self):
            assert self.timeout == 5
            raise TimeoutError("loopback blocked")

        def close(self):
            self.closed = True

    sockets = [FakeSocket() for _ in range(6)]
    created = iter(sockets)
    monkeypatch.setattr(platform_compat.socket, "socket", lambda *_args: next(created))
    with pytest.raises(TimeoutError):
        _bounded_socketpair(socket.AF_INET)
    assert all(item.closed for item in sockets)
