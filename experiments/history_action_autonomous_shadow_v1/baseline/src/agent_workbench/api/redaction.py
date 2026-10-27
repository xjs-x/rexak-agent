"""面向浏览器的只读投影统一脱敏；数据库和工件原件保持不变。"""

from __future__ import annotations

from typing import Any

from agent_workbench.evidence.redaction import redact_api_value as redact_api_value


def redact_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """旧的原始流式分片无法跨事件脱敏，因此浏览器不得读取其正文。"""
    if event_type == "message.delta" and payload.get("redaction_version") != 1:
        return {
            "role": "assistant",
            "delta": "[旧流式文本已隐藏，请查看对话归档]",
            "redaction_status": "legacy_hidden",
        }
    return redact_api_value(payload)
