"""每个并行工具各自绑定派发时的只读焦点，重放保留原记录。

关键语义（§5.4）：工具调用继承的是**发出这一批工具时已确定的焦点**，
不是某个并行工具写到一半的新状态。若模型把 `update_plan` 与执行工具放在同一批，
执行记录明确标为**原快照关联**，不能显示成已采用新计划。
"""

from contextvars import ContextVar
from typing import Any

execution_context: ContextVar[dict[str, Any] | None] = ContextVar("execution_context", default=None)


def context_from_state(
    state: dict[str, Any], *, same_batch_plan_update: bool = False
) -> dict[str, Any]:
    focus = state.get("current_focus") or {}
    context: dict[str, Any] = {
        "step_id": focus.get("step_id"),
        "lead_id": focus.get("lead_id"),
        "focus_source": focus.get("source"),
        "plan_revision": int(state.get("plan_revision") or 0),
        "association": "focus_snapshot" if focus else "exploration",
    }
    if same_batch_plan_update:
        context["same_batch_plan_update"] = True
        context["association_note"] = (
            "同一批里还有 update_plan；本记录只与**原快照**关联，不代表已采用新计划。"
        )
    return context


def batch_tool_names(state: dict[str, Any]) -> list[str]:
    """返回产生这一批工具调用的模型消息里的工具名（用于同批关联标注）。"""
    messages = state.get("messages") or []
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None)
        if calls:
            return [str(call.get("name") or "") for call in calls]
    return []


__all__ = ["batch_tool_names", "context_from_state", "execution_context"]
