"""决策前刷新只读领域投影；实际展示后才推进 checkpoint 召回游标。

本中间件同时负责**焦点自动承接**（§5.2）：在装配前用与 `update_plan` 相同的纯函数
解析焦点，并用同一解析结果同时构造只读黑板与返回图更新 —— 避免两处推导出不同焦点。
"""

from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.agent.working_context import assemble_working_context


class WorkingContextMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        values = cast("dict[str, Any]", dict(state or {}))
        # 没有显式选择时，按"唯一进行中步骤"承接焦点；多主线时不乱猜。
        focus, hint = resolve_focus(
            values.get("plan"), values.get("current_focus"), explicit_focus=None
        )
        changed = focus_signature(focus) != focus_signature(values.get("current_focus"))
        values["current_focus"] = focus
        async with context.toolbox.db.session() as db:
            view = await assemble_working_context(db, context.toolbox.session_id, values)
        context.toolbox.working_context = view
        update: dict[str, object] = {
            "active_lead_ids": view["next"]["selected_ids"],
            "pending_job_ids": view["pending_job_ids"],
        }
        if changed:
            update["current_focus"] = focus
        if hint and not values.get("progress_feedback"):
            # 只给一次简短提示；不覆盖进度中间件已经给出的策略反馈。
            update["progress_feedback"] = hint
        return update

    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = state
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        manifest = context.toolbox.context_manifest or {}
        view = manifest.get("working_context") or {}
        selected = view.get("next") or {}
        if not selected.get("review_due"):
            return None
        return {
            "review_cursor": selected.get("cursor_after_selection"),
            "last_review_turn": selected.get("turn", 0),
            "last_review_source_signature": selected.get("source_signature"),
        }
