"""完整工具轮次后的正常分段，状态更新与官方跳转一起提交 checkpoint。"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState

YIELD_REASON = "completed_round_budget"


class SegmentSchedulingError(RuntimeError):
    """片段状态没有推进或不满足合法边界；不能忙循环或冒充任务结论。"""


def completed_model_message(state: Any) -> AIMessage | None:
    pending: set[str] = set()
    latest: AIMessage | None = None
    for message in state.get("messages", []):
        if isinstance(message, AIMessage):
            latest = message
            for call in message.tool_calls:
                if not call["id"]:
                    return None
                pending.add(call["id"])
        elif isinstance(message, ToolMessage):
            pending.discard(message.tool_call_id)
    return latest if latest is not None and latest.id and not pending else None


class SegmentMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(self, completed_round_limit: int = 4) -> None:
        super().__init__()
        if not 1 <= completed_round_limit <= 5:
            raise ValueError("锁定 middleware 拓扑在 60 个图步数内每片段允许 1–5 个完整决策轮次")
        self.completed_round_limit = completed_round_limit

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        context = getattr(runtime, "context", None)
        check = getattr(getattr(context, "toolbox", None), "assert_control_current", None)
        if check is not None:
            await check()
        message = completed_model_message(state)
        if message is None:
            return None
        update = {}
        rounds = int(state.get("segment_completed_rounds") or 0)
        if str(message.id) != state.get("last_completed_model_message_id"):
            rounds += 1
            update.update(
                segment_completed_rounds=rounds,
                completed_decision_rounds=int(state.get("completed_decision_rounds") or 0) + 1,
                last_completed_model_message_id=str(message.id),
            )
        if rounds >= self.completed_round_limit:
            update.update(segment_yield_reason=YIELD_REASON, jump_to="end")
        return update or None
