"""决策前刷新只读领域投影；实际展示后才推进 checkpoint 召回游标。"""

from langchain.agents.middleware import AgentMiddleware

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.agent.working_context import assemble_working_context


class WorkingContextMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        async with context.toolbox.db.session() as db:
            view = await assemble_working_context(db, context.toolbox.session_id, dict(state))
        context.toolbox.working_context = view
        return {
            "active_lead_ids": view["next"]["selected_ids"],
            "pending_job_ids": view["pending_job_ids"],
        }

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
