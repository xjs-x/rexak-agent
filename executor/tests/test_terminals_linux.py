"""PTY 真终端演练（验收 D01/D03/D06/D08 的执行端部分）。

必须在 Linux 上运行（forkpty / termios）。Windows 上 skip；等价覆盖由
`scripts/accept_execution_runtime.py --mode isolated` 在真实服务上执行。

这里直接驱动 `TerminalRuntime`（与生产同一实现），只把"谁来发起"换成测试。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from workbench_executor._generated.contracts import execution_runtime as proto
from workbench_executor.ledger import Ledger
from workbench_executor.settings import ExecutorSettings
from workbench_executor.terminals import TerminalRuntime

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="forkpty / termios 只在 Linux 上存在；Windows 上跑等于没验证",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_runtime(tmp_path: Path) -> tuple[TerminalRuntime, Ledger, ExecutorSettings]:
    settings = ExecutorSettings(
        data_root=tmp_path / "root",
        workspace_root=tmp_path / "root" / "workspaces",
        execution_mode="trusted",
    )
    settings.ensure_directories()
    ledger = Ledger(settings.ledger_path)
    run(ledger.open())
    return TerminalRuntime(settings, ledger, "boot-test"), ledger, settings


def envelope(action: str, arguments: dict[str, Any], *, operation_id: str) -> Any:
    return proto.ExecutionOperationEnvelope(
        operation_id=operation_id,
        session_id="00000000-0000-0000-0000-000000000001",
        run_id="00000000-0000-0000-0000-000000000002",
        tool_call_id=f"call-{operation_id}",
        request_digest=f"digest-{operation_id}",
        action=action,
        arguments=arguments,
        owner_generation=1,
        dispatch_control_version=1,
        session_control_seq=1,
    )


def test_interactive_shell_send_read_and_prompt(tmp_path: Path) -> None:
    runtime, ledger, _settings = make_runtime(tmp_path)

    async def scenario() -> None:
        try:
            receipt, reason = await runtime.open(
                envelope(
                    "terminal_open",
                    {
                        "argv": ["/bin/sh"],
                        "mode": "interactive",
                        "prompt_regex": r"\$ $",
                        "quiet_ms": 300,
                        "startup_grace_ms": 3000,
                    },
                    operation_id="op-open",
                ),
                arguments={
                    "argv": ["/bin/sh"],
                    "mode": "interactive",
                    "prompt_regex": r"\$ $",
                    "quiet_ms": 300,
                    "startup_grace_ms": 3000,
                    "rows": 24,
                    "cols": 80,
                },
            )
            terminal_id = receipt.terminal_id
            assert terminal_id
            assert reason in {"prompt", "quiet", "output", "wait_timeout", "exited"}

            send = await runtime.send(
                envelope(
                    "terminal_send",
                    {"terminal_id": terminal_id},
                    operation_id="op-send-1",
                ),
                proto.TerminalSendArguments(
                    terminal_id=terminal_id,
                    text="echo MARKER_ABC",
                    submit=True,
                    wait=True,
                    wait_ms=3000,
                    prompt_regex=r"\$ $",
                    quiet_ms=300,
                ),
            )
            assert send.status == "succeeded"
            page = await runtime.read_page(
                terminal_id, cursor=None, stream="stdout", limit=65536
            )
            assert "MARKER_ABC" in page.text
            # 再次发送：状态延续（同一 terminal）
            send2 = await runtime.send(
                envelope(
                    "terminal_send",
                    {"terminal_id": terminal_id},
                    operation_id="op-send-2",
                ),
                proto.TerminalSendArguments(
                    terminal_id=terminal_id,
                    text="echo SECOND",
                    submit=True,
                    wait=True,
                    wait_ms=3000,
                    prompt_regex=r"\$ $",
                ),
            )
            assert send2.status == "succeeded"
            # 相同 operation_id 重试：返回原回执，不重复写入
            retry = await runtime.send(
                envelope(
                    "terminal_send",
                    {"terminal_id": terminal_id},
                    operation_id="op-send-1",
                ),
                proto.TerminalSendArguments(terminal_id=terminal_id, text="echo MARKER_ABC", submit=True),
            )
            assert retry.deduplicated is True
            page2 = await runtime.read_page(
                terminal_id, cursor=page.next_cursor, stream="stdout", limit=65536
            )
            assert page2.byte_count >= 0
            # close 幂等
            close = await runtime.close(
                envelope("terminal_close", {"terminal_id": terminal_id}, operation_id="op-close"),
                proto.TerminalCloseArguments(terminal_id=terminal_id),
            )
            assert close.status in {"succeeded", "unknown"}
            again = await runtime.close(
                envelope("terminal_close", {"terminal_id": terminal_id}, operation_id="op-close-2"),
                proto.TerminalCloseArguments(terminal_id=terminal_id),
            )
            assert "重复" in again.note or again.status == "succeeded"
        finally:
            await runtime.shutdown()
            await ledger.close()

    run(scenario())


def test_program_exit_is_reported_and_not_confused_with_quiet(tmp_path: Path) -> None:
    runtime, ledger, _settings = make_runtime(tmp_path)

    async def scenario() -> None:
        try:
            receipt, _reason = await runtime.open(
                envelope(
                    "terminal_open",
                    {"argv": ["/bin/sh", "-c", "echo bye; sleep 0.2; exit 7"]},
                    operation_id="op-open-exit",
                ),
                arguments={
                    "command": "echo bye; sleep 0.2; exit 7",
                    "mode": "hands_free",
                    "quiet_ms": 200,
                    "startup_grace_ms": 500,
                    "rows": 24,
                    "cols": 80,
                },
            )
            terminal_id = receipt.terminal_id
            await asyncio.sleep(1.5)
            status = await runtime.status(terminal_id)
            assert status is not None
            assert status.state.value == "exited"
            assert status.interaction_state.value == "not_applicable"
            row = await ledger.get_terminal(terminal_id)
            assert row is not None and row["exit_code"] == 7
        finally:
            await runtime.shutdown()
            await ledger.close()

    run(scenario())


def test_resize_and_screen(tmp_path: Path) -> None:
    runtime, ledger, _settings = make_runtime(tmp_path)

    async def scenario() -> None:
        try:
            receipt, _reason = await runtime.open(
                envelope(
                    "terminal_open",
                    {"argv": ["/bin/sh"], "mode": "hands_free"},
                    operation_id="op-open-resize",
                ),
                arguments={
                    "argv": ["/bin/sh"],
                    "mode": "hands_free",
                    "rows": 24,
                    "cols": 80,
                    "quiet_ms": 200,
                    "startup_grace_ms": 500,
                },
            )
            terminal_id = receipt.terminal_id
            resize = await runtime.resize(
                envelope(
                    "terminal_resize",
                    {"terminal_id": terminal_id, "rows": 30, "cols": 100},
                    operation_id="op-resize",
                ),
                proto.TerminalResizeArguments(terminal_id=terminal_id, rows=30, cols=100),
            )
            assert resize.status == "succeeded"
            await runtime.send(
                envelope(
                    "terminal_send",
                    {"terminal_id": terminal_id},
                    operation_id="op-send-screen",
                ),
                proto.TerminalSendArguments(
                    terminal_id=terminal_id,
                    text="printf 'AAA\\nBBB\\n'",
                    submit=True,
                    wait=True,
                    wait_ms=2000,
                    quiet_ms=300,
                ),
            )
            page = await runtime.read_page(
                terminal_id, cursor=None, stream="stdout", limit=65536, view="screen"
            )
            assert "AAA" in page.text and "BBB" in page.text
            row = await ledger.get_terminal(terminal_id)
            assert row is not None and row["rows"] == 30 and row["cols"] == 100
        finally:
            await runtime.shutdown()
            await ledger.close()

    run(scenario())


def test_cross_session_access_is_rejected(tmp_path: Path) -> None:
    runtime, ledger, _settings = make_runtime(tmp_path)

    async def scenario() -> None:
        try:
            receipt, _ = await runtime.open(
                envelope(
                    "terminal_open",
                    {"argv": ["/bin/sh"], "mode": "hands_free"},
                    operation_id="op-open-owner",
                ),
                arguments={"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200},
            )
            other = proto.ExecutionOperationEnvelope(
                operation_id="op-other",
                session_id="00000000-0000-0000-0000-0000000000ff",
                run_id="00000000-0000-0000-0000-000000000002",
                tool_call_id="call-other",
                request_digest="digest-other",
                action="terminal_send",
                arguments={"terminal_id": receipt.terminal_id},
            )
            with pytest.raises(Exception) as excinfo:
                await runtime.send(
                    other,
                    proto.TerminalSendArguments(terminal_id=receipt.terminal_id, text="x"),
                )
            assert "会话" in str(excinfo.value)
        finally:
            await runtime.shutdown()
            await ledger.close()

    run(scenario())


def test_concurrent_inputs_and_write_before_receipt_failure(tmp_path, monkeypatch):
    runtime, ledger, _ = make_runtime(tmp_path)

    async def scenario():
        try:
            receipt, _ = await runtime.open(envelope("terminal_open", {}, operation_id="edge-open"),
                arguments={"argv": ["/bin/sh"], "mode": "hands_free", "quiet_ms": 200})
            tid = receipt.terminal_id
            assert tid
            async def send(index):
                args = proto.TerminalSendArguments(terminal_id=tid, text=f"echo item-{index} >> serial.txt", submit=True)
                return await runtime.send(envelope("terminal_send", {}, operation_id=f"edge-{index}"), args)
            results = await asyncio.gather(send(1), send(2))
            assert all(item.status == "succeeded" for item in results)
            original = runtime._write_all
            calls = 0
            async def fail_after_write(session, data):
                nonlocal calls
                calls += 1
                await original(session, data)
                raise RuntimeError("fixture: bytes written before receipt")
            monkeypatch.setattr(runtime, "_write_all", fail_after_write)
            args = proto.TerminalSendArguments(terminal_id=tid, text="echo once >> serial.txt", submit=True)
            env = envelope("terminal_send", {}, operation_id="edge-uncertain")
            with pytest.raises(RuntimeError, match="bytes written"):
                await runtime.send(env, args)
            retried = await runtime.send(env, args)
            assert retried.status == "unknown" and retried.deduplicated
            assert calls == 1
            await asyncio.sleep(0.4)
            path = Path(runtime._sessions[tid].cwd) / "serial.txt"
            assert sorted(path.read_text(encoding="utf-8").splitlines()) == ["item-1", "item-2", "once"]
        finally:
            await runtime.shutdown()
            await ledger.close()
    run(scenario())
