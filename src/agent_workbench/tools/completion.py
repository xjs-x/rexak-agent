"""可选显式结束工具；与普通最终回复共用结束一致性检查。"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from agent_workbench.agent.context import RunContext


@tool("propose_completion")
async def propose_completion(
    runtime: ToolRuntime[RunContext],
    goal_answer: str,
    reason: Literal["goal_answered", "exploration_converged"] = "goal_answered",
    limitations: str = "",
    evidence_refs: list[str] | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """结束当前任务并提交最终回答，也可以直接用普通最终回复结束。

    Args:
        goal_answer: 对用户目标的直接回答；候选问题不能写成已验证漏洞。
        reason: 目标已回答或当前可达范围内探索收敛。
        limitations: 尚未验证的问题、测试条件与其他局限，可省略。
        evidence_refs: 可选真实执行工件 ID；程序检查会话、文件和哈希。
        tool_call_id: 框架注入。

    未决执行或用户指令仍由 Runner 核对；无需关闭全部计划或假设。
    """
    if not goal_answer.strip():
        raise ValueError("最终回答不能为空")
    payload = {
        "reason": reason,
        "goal_answer": goal_answer,
        "limitations": limitations,
        "evidence_refs": list(evidence_refs or []),
        "proposed_at_turn": len(runtime.state.get("messages") or []),
    }
    return Command(
        update={
            "completion_proposal": payload,
            "messages": [
                ToolMessage(
                    content=json.dumps(
                        {
                            "ok": True,
                            "proposal": payload,
                            "note": "已保存结束信号；待控制与执行状态核对。",
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id=tool_call_id,
                    name="propose_completion",
                )
            ],
        }
    )


COMPLETION_TOOLS = [propose_completion]
