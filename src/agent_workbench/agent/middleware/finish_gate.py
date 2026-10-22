"""旧 checkpoint 节点名兼容桥；不审查、不生成结束提议或模型反馈。"""

from langchain.agents.middleware import AgentMiddleware, hook_config

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState


class FinishGateMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    """只在旧图恢复时装配，保留节点边界后沿官方边继续。"""

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = state, runtime
        return

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = state, runtime
        return
