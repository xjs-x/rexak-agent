"""刷新有界的只读事实目录；不进行领域复查、主动提醒或判断填表。"""

from langchain.agents.middleware import AgentMiddleware

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.agent.middleware.segment import completed_model_message
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.agent.working_context import assemble_basic_context
from agent_workbench.domain.graph_sync import graph_context


class WorkingContextMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        focus, _ = resolve_focus(state.get("plan"), state.get("current_focus"), None)
        async with context.toolbox.db.session() as db:
            view = await assemble_basic_context(db, context.toolbox.session_id, dict(state),
                                                legacy_memory=not context.toolbox.settings.session_graph_enabled)
            from agent_workbench.domain.attachments import TRUST_NOTE, list_metadata
            from agent_workbench.evidence.artifacts import ArtifactStore

            attachments = await list_metadata(db, context.toolbox.session_id,
                context.toolbox.settings, ArtifactStore(context.toolbox.db.settings), limit=5)
            if attachments["attachments"]:
                view["user_attachments"] = {**attachments, "note": TRUST_NOTE,
                    "read_hint": "需要正文时主动调用 list_attachments/read_attachment/search_attachment；目录只含元数据。"}
        if (context.toolbox.settings.session_graph_enabled
            and getattr(context.toolbox.settings, "decision_memory_mode", "current") == "current"):
            view["session_memory"] = await graph_context(context.toolbox, completed_model_message(state))
        elif getattr(context.toolbox.settings, "decision_memory_mode", "current") == "blackboard_v1":
            context.toolbox.graph_review_window_id = None
            context.toolbox.graph_sync_snapshot = None
        context.toolbox.working_context = view
        update = {"pending_job_ids": view["pending_job_ids"]}
        if focus_signature(focus) != focus_signature(state.get("current_focus")):
            update["current_focus"] = focus
        return update

    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = state, runtime
        # 保留旧 checkpoint 的节点名，恢复时不再推进旧领域协议游标。
        return
