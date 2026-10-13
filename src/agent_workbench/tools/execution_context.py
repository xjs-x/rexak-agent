"""每个并行工具各自绑定派发时的只读焦点，重放保留原记录。"""

from contextvars import ContextVar
from typing import Any

execution_context: ContextVar[dict[str, Any] | None] = ContextVar("execution_context", default=None)


def context_from_state(state: dict[str, Any]) -> dict[str, Any]:
    focus = state.get("current_focus") or {}
    return {
        "step_id": focus.get("step_id"),
        "lead_id": focus.get("lead_id"),
        "plan_revision": int(state.get("plan_revision") or 0),
        "association": "focus" if focus else "exploration",
    }
