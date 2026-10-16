"""终端输入构造与校验（验收 D02 的前置校验部分；可移植）。

顺序固定：text -> submit 回车 -> keys -> hex -> paste；
任何非法参数在**写入之前**整体拒绝。
"""

from __future__ import annotations

import pytest
from workbench_executor._generated.contracts import execution_runtime as proto
from workbench_executor.terminals import TerminalRejected, TerminalRuntime


def _send(**kwargs) -> proto.TerminalSendArguments:
    base = {"terminal_id": "t-1"}
    base.update(kwargs)
    return proto.TerminalSendArguments(**base)


def test_input_order_is_fixed():
    data = TerminalRuntime.build_input_bytes(
        _send(text="cmd", submit=True, keys=["Tab"], hex=["41"], paste="p")
    )
    expected = (
        b"cmd"
        + proto.KEY_BYTES["Enter"]
        + proto.KEY_BYTES["Tab"]
        + b"A"
        + proto.BRACKETED_PASTE_START
        + b"p"
        + proto.BRACKETED_PASTE_END
    )
    assert data == expected


def test_text_preserves_whitespace_and_control_bytes():
    data = TerminalRuntime.build_input_bytes(_send(text="  a\tb\n", hex=["03"]))
    assert data == b"  a\tb\n\x03"


def test_unknown_keys_and_invalid_hex_rejected_as_a_whole():
    with pytest.raises(TerminalRejected) as excinfo:
        TerminalRuntime.build_input_bytes(_send(text="echo", keys=["F13"]))
    assert "未知按键" in excinfo.value.message
    with pytest.raises(TerminalRejected):
        TerminalRuntime.build_input_bytes(_send(hex=["zz"]))
    with pytest.raises(TerminalRejected):
        TerminalRuntime.build_input_bytes(_send(hex=["00"] * (proto.MAX_HEX_ITEMS + 1)))
    with pytest.raises(TerminalRejected):
        # 单次 hex 输入超过字节上限（"41" 是一个字节的十六进制写法）
        TerminalRuntime.build_input_bytes(
            _send(hex=["41" * (proto.MAX_HEX_BYTES + 1)])
        )
    # 恰好在上限内：允许
    allowed = TerminalRuntime.build_input_bytes(
        _send(hex=["41" * proto.MAX_HEX_BYTES])
    )
    assert len(allowed) == proto.MAX_HEX_BYTES


def test_supported_keys_cover_required_set():
    for key in (
        "Enter",
        "Tab",
        "Esc",
        "Backspace",
        "Delete",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Ctrl-C",
        "Ctrl-D",
    ):
        assert key in proto.SUPPORTED_KEYS
        data = TerminalRuntime.build_input_bytes(_send(keys=[key]))
        assert data == proto.KEY_BYTES[key]


def test_paste_uses_bracketed_protocol_and_is_honest():
    data = TerminalRuntime.build_input_bytes(_send(paste="line1\nline2"))
    assert data.startswith(proto.BRACKETED_PASTE_START)
    assert data.endswith(proto.BRACKETED_PASTE_END)
    assert b"line1\nline2" in data


def test_empty_input_is_empty_bytes():
    assert TerminalRuntime.build_input_bytes(_send()) == b""
