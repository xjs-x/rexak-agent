"""Launcher boundaries: private configuration, serial isolation, and safe stopping."""

from __future__ import annotations

import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_workbench.contracts.delegation import ExecutionMode
from agent_workbench.tsecbench.config import BenchmarkConfig
from agent_workbench.tsecbench.flow import FlowOutcome
from agent_workbench.tsecbench.launcher import (
    console_url,
    evaluate_serial,
    save_profile,
    validate_inputs,
    validate_vpn,
)
from agent_workbench.tsecbench.platform import ChallengeInfo


async def test_console_timeout_on_absent_api_still_requests_guarded_startup(tmp_path, monkeypatch):
    import httpx

    from agent_workbench.tsecbench import launcher

    actual_client = httpx.AsyncClient

    def unavailable(request):
        raise httpx.ConnectTimeout("local listener unavailable", request=request)

    monkeypatch.setattr(
        launcher.httpx,
        "AsyncClient",
        lambda **kwargs: actual_client(**kwargs, transport=httpx.MockTransport(unavailable)),
    )

    def spawn(args, **_kwargs):
        assert "--api-only" in args
        raise RuntimeError("guarded startup requested")

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="guarded startup requested"):
        await launcher.ConsoleProcess().start(BenchmarkConfig(env_file=str(tmp_path / "test.env")))


def test_launcher_cancel_targets_current_run_and_stops_future_questions(monkeypatch):
    import scripts.start_tsecbench as ui

    from agent_workbench.tsecbench import workbench_api

    cancelled = []

    class Api:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def cancel_run(self, run):
            cancelled.append(run)

    monkeypatch.setattr(workbench_api, "HttpWorkbenchApi", Api)
    monkeypatch.setattr(ui.threading, "Thread", lambda target, **_k: SimpleNamespace(start=target))
    window: Any = ui.EvaluationWindow.__new__(ui.EvaluationWindow)
    window.stop = threading.Event()
    window.cancel_button = SimpleNamespace(configure=lambda **_k: None)
    window.status = SimpleNamespace(set=lambda _v: None)
    window.current_run = "current-run"
    window.config = BenchmarkConfig()
    window.emit = lambda *_args: None
    window.cancel_current()
    assert window.stop.is_set() and window.cancel_requested
    assert cancelled == ["current-run"]


def test_window_close_requests_cancellation_and_waits_for_cleanup(monkeypatch):
    import scripts.start_tsecbench as ui

    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *_a, **_k: True)
    window: Any = ui.EvaluationWindow.__new__(ui.EvaluationWindow)
    window.busy = window.running = True
    window.root = None
    calls = []
    window.cancel_current = lambda: calls.append("cancel")
    window.close()
    assert window.exit_after_run and calls == ["cancel"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OpenVPN command interface")
def test_missing_gui_rescan_failure_and_retry_do_not_block_connect(tmp_path, monkeypatch):
    from agent_workbench.tsecbench import launcher

    home = tmp_path / "user"
    program_files = tmp_path / "programs"
    exe = program_files / "OpenVPN/bin/openvpn-gui.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    source = tmp_path / "profile/client.ovpn"
    source.parent.mkdir()
    source.write_text("client\nremote localhost 9798\n<key>\nx\n</key>\n", encoding="utf-8")
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(launcher, "restrict_path", lambda *_a, **_k: None)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    requested = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **_k: requested.append(args))
    cfg = BenchmarkConfig(ovpn_path=str(source))
    assert launcher.connect_windows_vpn(cfg) == launcher.connect_windows_vpn(cfg)
    assert len(requested) == 2
    assert all(args[-3:] == ["--command", "connect", "rexak-eval-profile"] for args in requested)
    destination = home / "OpenVPN/config/rexak-eval-profile.ovpn"
    destination.write_text("preserve-different-profile", encoding="utf-8")
    with pytest.raises(RuntimeError, match="内容不同"):
        launcher.connect_windows_vpn(cfg)
    assert destination.read_text(encoding="utf-8") == "preserve-different-profile"


def challenge(code: str = "one", **changes: Any) -> ChallengeInfo:
    row = ChallengeInfo(code, "test", "easy", "1", 100, 1, 0, False, "stopped", ())
    return replace(row, **changes)


def test_new_profiles_preserve_old_credentials_and_vpn(tmp_path):
    source = tmp_path / "download.ovpn"
    original = b"client\nremote 127.0.0.1 9798\n<key>\nsynthetic-key\n</key>\n"
    source.write_bytes(original)
    first = save_profile(
        "https://benchmark.example.com",
        "first",
        source,
        ExecutionMode.ULTRA,
        600,
        root=tmp_path / "private",
    )
    second = save_profile(
        "https://benchmark.example.com",
        "second",
        source,
        ExecutionMode.STANDARD,
        120,
        root=tmp_path / "private",
    )
    assert first.env_file != second.env_file
    assert first.env_file is not None and second.env_file is not None
    assert first.ovpn_path is not None
    assert '"first"' in Path(first.env_file).read_text(encoding="utf-8")
    assert '"second"' in Path(second.env_file).read_text(encoding="utf-8")
    assert Path(first.ovpn_path).read_bytes() == source.read_bytes() == original
    assert not Path(first.env_file).read_bytes().startswith(b"\xef\xbb\xbf")
    assert first.execution_mode is ExecutionMode.ULTRA
    from agent_workbench.tsecbench.launcher import last_profile_fields

    restored = last_profile_fields(root=tmp_path / "private")
    assert restored["BENCHMARK_TOKEN"] == "second"
    assert restored["WORKBENCH_TSECBENCH_OVPN"] == second.ovpn_path
    assert restored["TSECBENCH_EXECUTION_MODE"] == "standard"
    assert restored["TSECBENCH_MAX_SECONDS"] == "120"


@pytest.mark.parametrize(
    "url,token,minutes",
    [
        ("https://name:password@example.com", "x", "1"),
        ("https://example.com/?token=secret", "x", "1"),
        ("https://example.com", "x\nBAD=value", "1"),
        ("https://example.com", "", "1"),
        ("https://example.com", "x", "nan"),
        ("https://example.com", "x", "0"),
    ],
)
def test_configuration_rejects_unsafe_or_invalid_fields(url, token, minutes):
    with pytest.raises(ValueError):
        validate_inputs(url, token, minutes)


@pytest.mark.parametrize(
    "hook",
    ["up /tmp/script", '"plugin" /tmp/plugin', "<connection>\nup /tmp/script\n</connection>"],
)
def test_vpn_does_not_execute_downloaded_hooks(tmp_path, hook):
    path = tmp_path / "bad.ovpn"
    path.write_text("client\nremote localhost 9798\n<key>\nx\n</key>\n" + hook, encoding="utf-8")
    with pytest.raises(ValueError, match="指令"):
        validate_vpn(path)


def test_console_link_targets_exact_session_without_credentials():
    cfg = BenchmarkConfig(token="private-value")
    url = console_url(cfg, "ae154fc4-2618-4807-9d9e-35320d27193a")
    assert url.endswith("/console/?session=ae154fc4-2618-4807-9d9e-35320d27193a")
    assert "private-value" not in url
    with pytest.raises(ValueError):
        console_url(cfg, "bad&token=secret")


class Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def run_case(rows, *, status="partial", settled=True, stop_after=False):
    stop = threading.Event()
    calls = []
    events = []
    state = {"rows": rows}

    class Platform(Context):
        def __init__(self, _config):
            pass

        async def list_challenges(self):
            return state["rows"]

    class Api(Context):
        def __init__(self, _config):
            pass

    def flow_factory(**kwargs):
        async def run():
            code = kwargs["options"].unique_code
            calls.append(code)
            kwargs["on_run_created"]("session-" + code, "run-" + code)
            if stop_after:
                stop.set()
            return FlowOutcome(outcome=status, unique_code=code, settled=settled)

        return SimpleNamespace(run=run)

    outcomes = await evaluate_serial(
        BenchmarkConfig(),
        ["one", "two"],
        stop,
        lambda kind, value: events.append((kind, value)),
        platform_factory=cast(Any, Platform),
        api_factory=cast(Any, Api),
        flow_factory=cast(Any, flow_factory),
    )
    return calls, events, outcomes


async def test_batch_serializes_existing_flows_and_publishes_session_ids():
    calls, events, outcomes = await run_case([challenge(), challenge("two")])
    assert calls == ["one", "two"]
    assert len(outcomes) == 2
    assert [value for kind, value in events if kind == "session"] == [
        ("session-one", "run-one"),
        ("session-two", "run-two"),
    ]


@pytest.mark.parametrize(
    "status,settled", [("failed", True), ("cancelled", True), ("partial", False), ("blocked", True)]
)
async def test_batch_does_not_dispatch_after_failure_cancel_or_uncertain_execution(status, settled):
    calls, _events, _outcomes = await run_case(
        [challenge(), challenge("two")], status=status, settled=settled
    )
    assert calls == ["one"]


async def test_stop_after_current_does_not_cancel_current_or_start_next():
    calls, _events, outcomes = await run_case([challenge(), challenge("two")], stop_after=True)
    assert calls == ["one"]
    assert outcomes[0].outcome == "partial"


async def test_active_container_is_not_reused_or_closed_by_new_batch():
    with pytest.raises(RuntimeError, match="未释放"):
        await run_case([challenge(container_status="available"), challenge("two")])


async def test_completed_challenge_is_skipped_on_refreshed_platform_state():
    calls, _events, _outcomes = await run_case([challenge(is_completed=True), challenge("two")])
    assert calls == ["two"]
