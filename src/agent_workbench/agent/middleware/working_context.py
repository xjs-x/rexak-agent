"""刷新有界的只读事实目录；不进行领域复查、主动提醒或判断填表。"""

from langchain.agents.middleware import AgentMiddleware

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.agent.working_context import assemble_basic_context


class WorkingContextMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        focus, _ = resolve_focus(state.get("plan"), state.get("current_focus"), None)
        async with context.toolbox.db.session() as db:
            view = await assemble_basic_context(db, context.toolbox.session_id, dict(state))
        context.toolbox.working_context = view
        update = {"pending_job_ids": view["pending_job_ids"]}
        if focus_signature(focus) != focus_signature(state.get("current_focus")):
            update["current_focus"] = focus
        return update

    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = state, runtime
        # 保留旧 checkpoint 的节点名，恢复时不再推进旧领域协议游标。
        return
