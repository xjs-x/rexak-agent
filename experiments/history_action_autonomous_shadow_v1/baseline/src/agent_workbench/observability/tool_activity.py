"""有界、脱敏的工具调用展示事件；执行状态仍以操作与 Executor 台账为准。"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any

from agent_workbench.contracts.events import EventType
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.observability.events import append_event


def visible_text(value: Any, limit: int = 8192) -> str:
    """先解析 JSON 再逐字段脱敏，最后截断；不把完整长正文写入事件。"""
    if isinstance(value, str):
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(value)
    value = redact_api_value(value)
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return rendered if len(rendered) <= limit else rendered[:limit] + "\n[展示内容已截断]"


def result_view(result: Any) -> tuple[str, str]:
    content = getattr(result, "content", None)
    if content is None:
        # Command 返回的是图状态更新；不能把其完整消息历史投影为工具输出。
        return "completed", "工具已返回图状态更新"
    parsed: Any = content
    if isinstance(content, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(content)
    status = str(parsed.get("status") or "completed") if isinstance(parsed, dict) else "completed"
    if getattr(result, "status", None) == "error":
        status = "failed"
    return status, visible_text(parsed)


async def emit_tool_activity(
    toolbox: Any, call: Mapping[str, Any], *, phase: str, status: str, output: str = ""
) -> None:
    async with toolbox.db.transaction() as db:
        await append_event(
            db,
            session_id=toolbox.session_id,
            run_id=toolbox.run_id,
            event_type=EventType.TOOL_ACCEPTED if phase == "started" else EventType.TOOL_COMPLETED,
            payload={
                "scope": "tool_activity",
                "tool_call_id": str(call.get("id") or ""),
                "tool": str(call.get("name") or "unknown"),
                "phase": phase,
                "status": status,
                "request_text": visible_text(call.get("args") or {}) if phase == "started" else "",
                "result_text": output,
            },
        )
