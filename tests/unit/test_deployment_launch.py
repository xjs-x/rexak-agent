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


@pytest.mark.parametrize(
    "choice,provider,key_name",
    [
        ("1", "xiaomi", "XIAOMI_API_KEY"),
        ("2", "bailian", "DASHSCOPE_API_KEY"),
    ],
)
def test_launcher_selects_provider_without_reusing_or_exposing_other_key(
    monkeypatch,
    capsys,
    choice,
    provider,
    key_name,
):
    settings = isolated_settings(
        DASHSCOPE_BASE_URL="https://fixture.invalid/compatible-mode/v1",
        XIAOMI_API_KEY="fixture-old-xiaomi",
        DASHSCOPE_API_KEY="fixture-old-bailian",
    )
    choices = iter(["invalid", choice])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    prompts = []

    def enter_key(prompt):
        prompts.append(prompt)
        return "fixture-selected-key"

    monkeypatch.setattr(start_workbench.getpass, "getpass", enter_key)
    selected, key = start_workbench.select_model(settings)
    assert selected == provider and key == "fixture-selected-key"
    assert ("小米" if provider == "xiaomi" else "阿里云百炼") in prompts[0]
    env = start_workbench.model_environment(
        settings,
        {
            "XIAOMI_API_KEY": "fixture-inherited",
            "DASHSCOPE_API_KEY": "fixture-inherited",
        },
    )
    assert env["XIAOMI_API_KEY"] == env["DASHSCOPE_API_KEY"] == ""
    assert key_name in env
    assert "fixture-" not in capsys.readouterr().out
    assert "fixture-old" not in repr(settings)


def test_empty_key_aborts_startup(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr(start_workbench.getpass, "getpass", lambda _: "  ")
    with pytest.raises(RuntimeError, match="未输入模型 Key"):
        start_workbench.select_model(isolated_settings())


@pytest.mark.parametrize(
    "choice,provider,key_name",
    [
        ("1", "xiaomi", "XIAOMI_API_KEY"),
        ("2", "bailian", "DASHSCOPE_API_KEY"),
    ],
)
def test_complete_launcher_passes_choice_to_api_and_only_selected_key_to_runner(
    tmp_path,
    monkeypatch,
    capsys,
    choice,
    provider,
    key_name,
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
    monkeypatch.setattr("builtins.input", lambda _: choice)
    monkeypatch.setattr(start_workbench.getpass, "getpass", lambda _: "fixture-selected-key")
    start_workbench.launch(api_only=False, check_only=False, no_browser=True, smoke_seconds=0.01)
    assert len(spawned) == len(stopped) == 2
    api_env, runner_env = spawned[0][1], spawned[1][1]
    assert (
        api_env["WORKBENCH_DEFAULT_PROVIDER"]
        == runner_env["WORKBENCH_DEFAULT_PROVIDER"]
        == provider
    )
    assert runner_env[key_name] == "fixture-selected-key"
    assert api_env["XIAOMI_API_KEY"] == api_env["DASHSCOPE_API_KEY"] == ""
    other_key = "DASHSCOPE_API_KEY" if provider == "xiaomi" else "XIAOMI_API_KEY"
    assert runner_env[other_key] == ""
    assert all(env["XIAOMI_API_KEY"] == env["DASHSCOPE_API_KEY"] == "" for _, env in checks)
    assert "fixture-selected-key" not in repr([argv for argv, _ in spawned])
    assert "fixture-selected-key" not in capsys.readouterr().out
    assert all(
        "fixture-selected-key" not in p.read_text(encoding="utf-8")
        for p in (cfg.log_root / "launcher").glob("*.log")
    )


def test_ipv6_api_url() -> None:
    assert start_workbench.base_url(Settings(api_host="::1", api_port=8770)) == "http://[::1]:8770"


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
