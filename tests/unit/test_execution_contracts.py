"""执行底座协议契约：保真、校验前置、枚举稳定（验收 A03 的协议面）。

要点：
- 命令正文、stdin、终端 text/paste **不做** strip，首尾空格/换行/控制字符原样保留；
- 非法输入在产生副作用之前被拒绝（未知动作、二选一约束、非法按键在构建期拒绝）；
- 枚举序列化稳定，跨进程/跨语言比较不依赖中文文本。
"""

from __future__ import annotations

import json

import pytest

from agent_workbench.contracts.execution_runtime import (
    EXECUTION_SUBPROTOCOL_VERSION,
    CommandArguments,
    ExecutionOperationEnvelope,
    OutputLimitAction,
    ResourceStatus,
    TerminalSendArguments,
    TerminationReason,
    WaitReason,
    WatchRule,
    validate_arguments,
)


def test_command_text_preserves_whitespace_and_control_characters():
    envelope = ExecutionOperationEnvelope(
        schema_version=EXECUTION_SUBPROTOCOL_VERSION,
        operation_id="op-1",
        session_id="s-1",
        run_id="r-1",
        tool_call_id="call-1",
        request_digest="a" * 64,
        action="run_command",
        arguments={
            "command": "  printf 'x\\n'  \n\ttail  ",
            "stdin_text": "  line1\nline2\t \n",
            "cwd": ".",
        },
    )
    payload = envelope.model_dump(mode="json")
    assert payload["arguments"]["command"] == "  printf 'x\\n'  \n\ttail  "
    assert payload["arguments"]["stdin_text"] == "  line1\nline2\t \n"
    # JSON 往返后仍然逐字节一致（序列化不做归一化）
    again = ExecutionOperationEnvelope.model_validate(json.loads(json.dumps(payload)))
    assert again.arguments["command"] == payload["arguments"]["command"]
    assert again.arguments["stdin_text"] == payload["arguments"]["stdin_text"]


def test_terminal_input_preserves_control_characters():
    arguments = TerminalSendArguments(
        terminal_id="t-1",
        text="a\tb ",
        submit=True,
        keys=["Ctrl-C"],
        hex=["1b", "5b41"],
        paste="line1\nline2\n",
    )
    dumped = arguments.model_dump(mode="json")
    assert dumped["text"] == "a\tb "
    assert dumped["paste"] == "line1\nline2\n"
    assert dumped["keys"] == ["Ctrl-C"]


def test_validate_arguments_rejects_both_forms_before_side_effects():
    with pytest.raises(ValueError):
        validate_arguments("run_command", {"command": "ls", "argv": ["ls"]})
    with pytest.raises(ValueError):
        validate_arguments("run_command", {})
    with pytest.raises(ValueError):
        validate_arguments("run_script", {"language": "python", "code": "x", "path": "x.py"})
    with pytest.raises(ValueError):
        validate_arguments("unknown_action", {})


def test_validate_arguments_accepts_valid_forms():
    parsed = validate_arguments("run_command", {"argv": ["/bin/echo", "a b"]})
    assert isinstance(parsed, CommandArguments)
    assert parsed.argv == ["/bin/echo", "a b"]
    script = validate_arguments("run_script", {"language": "python", "code": "print(1)"})
    assert getattr(script, "code", None) == "print(1)"


def test_watch_rule_limits_and_enum_values_are_stable():
    # 从 JSON 形态（字符串）构造：模型侧就是字符串枚举，这里验证解析与边界。
    rule = WatchRule.model_validate({"kind": "regex", "pattern": "login ok", "notify": "once"})
    assert rule.kind == "regex"
    assert rule.notify == "once"
    with pytest.raises(ValueError):
        WatchRule.model_validate({"kind": "regex", "pattern": ""})
    with pytest.raises(ValueError):
        WatchRule.model_validate({"kind": "regex", "pattern": "x" * 513})
    assert [item.value for item in ResourceStatus] == [
        "starting",
        "running",
        "exited",
        "closed",
        "unknown",
    ]
    assert TerminationReason.UNKNOWN.value == "unknown"
    assert WaitReason.WAIT_TIMEOUT.value == "wait_timeout"
    assert OutputLimitAction.PRESERVE_PROCESS.value == "preserve_process"


def test_envelope_identity_fields_must_not_be_empty():
    with pytest.raises(ValueError):
        ExecutionOperationEnvelope(
            operation_id=" ",
            session_id="s",
            run_id="r",
            tool_call_id="c",
            request_digest="d",
            action="run_command",
        )
