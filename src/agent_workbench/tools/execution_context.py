"""每个并行工具各自绑定派发时的只读焦点，重放保留原记录。

关键语义（§5.4）：工具调用继承的是**发出这一批工具时已确定的焦点**，
不是某个并行工具写到一半的新状态。若模型把 `update_plan` 与执行工具放在同一批，
执行记录明确标为**原快照关联**，不能显示成已采用新计划。

V3（方案 §8）：保留主 `lead_id`，同时附上**本步骤服务的全部线索**（`lead_ids`）
与**来源弱点的有效版本**（`source_weakness_revisions`），供"实际执行是否关联到该方向"的
验收使用。这些只是只读投影，不构成第二条权威记录。
"""

from contextvars import ContextVar
from typing import Any

execution_context: ContextVar[dict[str, Any] | None] = ContextVar("execution_context", default=None)


def focus_step(state: dict[str, Any]) -> dict[str, Any] | None:
    focus = state.get("current_focus") or {}
    step_id = focus.get("step_id")
    if not step_id:
        return None
    for step in state.get("plan") or []:
        if step.get("step_id") == step_id:
            return step
    return None


def context_from_state(
    state: dict[str, Any],
    *,
    same_batch_plan_update: bool = False,
    weakness_revisions: dict[str, int] | None = None,
    weakness_bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    focus = state.get("current_focus") or {}
    step = focus_step(state)
    lead_ids: list[str] = []
    if step is not None:
        from agent_workbench.agent.state import plan_step_lead_ids

        lead_ids = plan_step_lead_ids(step)
    context: dict[str, Any] = {
        "step_id": focus.get("step_id"),
        "lead_id": focus.get("lead_id"),
        "lead_ids": lead_ids,
        "focus_source": focus.get("source"),
        "plan_revision": int(state.get("plan_revision") or 0),
        "association": "focus_snapshot" if focus else "exploration",
    }
    if step is not None:
        from agent_workbench.agent.state import plan_step_kind

        context["step_kind"] = plan_step_kind(step)
    if weakness_revisions:
        context["source_weakness_revisions"] = {
            str(key): int(value) for key, value in weakness_revisions.items()
        }
    if weakness_bindings:
        context["source_weakness_bindings"] = list(weakness_bindings)
    if same_batch_plan_update:
        context["same_batch_plan_update"] = True
        context["association_note"] = (
            "同一批里还有 update_plan；本记录只与**原快照**关联，不代表已采用新计划。"
        )
    return context


def weakness_revisions_for_focus(toolbox: Any) -> dict[str, int]:
    """当前主线一跳关系里引用的来源弱点版本（只读投影）。"""
    view = getattr(toolbox, "working_context", None) or {}
    if "execution_sources" in view:
        return {str(key): int(value) for key, value in view["execution_sources"].items()}
    route = ((view.get("judgment") or {}).get("current_route")) or []
    out: dict[str, int] = {}
    for item in route:
        weakness_id = item.get("weakness_id")
        if not weakness_id:
            continue
        revision = item.get("source_weakness_revision")
        if revision is None:
            continue
        out[str(weakness_id)] = int(revision)
    return out


def batch_tool_names(state: dict[str, Any]) -> list[str]:
    """返回产生这一批工具调用的模型消息里的工具名（用于同批关联标注）。"""
    messages = state.get("messages") or []
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None)
        if calls:
            return [str(call.get("name") or "") for call in calls]
    return []


__all__ = [
    "batch_tool_names",
    "context_from_state",
    "execution_context",
    "focus_step",
    "weakness_revisions_for_focus",
]
