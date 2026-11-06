"""Optional work-plan tool exposed to the agent."""

from __future__ import annotations

from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.plan import PlanStepPatch


@tool("update_plan")
async def update_plan(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    expected_plan_revision: int,
    steps: list[PlanStepPatch] | None = None,
    focus: str | None = None,
    rationale: str | None = None,
) -> Command:
    """可选地局部更新工作计划或当前调查方向，不是工具执行的前提。

    Args:
        expected_plan_revision: 上下文中的当前版本；过期写入被拒绝。
        steps: 只提交要新增或修改的步骤。每项含 step_id，可选 description/status/rationale/lead_id。
            新步骤必须有 description。status 为 pending/in_progress/done/dropped/blocked。
            省略的步骤和字段保持原样；完成只表示工作记录，不代表漏洞验证。
        focus: 可选的简短调查方向，不依赖步骤或线索；空字符串清除焦点。
        rationale: 可选简短修订说明。
        tool_call_id: 框架注入的稳定调用 ID。

    无需为普通反思、切换或完成步骤提交证明。同批只能一次计划更新。
    """
    from agent_workbench.tools.planning import apply_plan_update

    return await apply_plan_update(
        runtime,
        tool_call_id,
        steps=steps,
        focus=focus,
        rationale=rationale,
        expected_plan_revision=expected_plan_revision,
    )


TASK_TOOLS = [update_plan]
