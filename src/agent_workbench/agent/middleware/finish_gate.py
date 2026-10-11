"""阶段结束判定（模型提出阶段结束时）。

流程（§6.5）：
1. 模型调用 `propose_completion`，写出结构化 `completion_proposal`；
2. 本中间件用**确定性检查**评估（见 `runtime/stopping.FinishGate`）；
3. 不通过 -> 把**具体反馈**写回状态并清除提议，让模型继续；
4. 模型用普通文本收尾但**未**形成提议 -> 最多给少量明确的修正反馈；
   重复不合格时标记"无法形成可靠结论"并暴露问题，**不无限调用同一提示**。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.observability.logging import get_logger
from agent_workbench.runtime.stopping import FinishGate

#: 允许的修正反馈次数上限。超过后停止空转并把问题暴露给用户。
MAX_FEEDBACK_ROUNDS = 3
log = get_logger("agent_workbench.finish_gate")


class FinishGateMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context: RunContext | None = getattr(runtime, "context", None)
        if context is None:
            return None
        toolbox = context.toolbox

        proposal = state.get("completion_proposal")
        attempts = int(state.get("finish_gate_attempts") or 0)
        messages = state.get("messages") or []

        if proposal is None:
            return await self._handle_plain_text_finish(state, attempts, messages)

        span = toolbox.spans.start(
            "finish_gate.middleware",
            session_id=toolbox.session_id,
            run_id=toolbox.run_id,
        )
        try:
            async with toolbox.db.session() as db:
                decision = await FinishGate(db).evaluate(
                    session_id=toolbox.session_id, proposal=proposal
                )
            span.finish()
            toolbox.spans.record(span, decision=decision.decision)
        except Exception as exc:
            span.finish(error=type(exc).__name__)
            raise
        finally:
            try:
                async with toolbox.db.transaction() as db:
                    await toolbox.spans.flush(db)
            except Exception as exc:
                log.warning(
                    "finish_gate_span_persist_failed",
                    run_id=str(toolbox.run_id),
                    error_type=type(exc).__name__,
                )

        if decision.allowed:
            # 通过：保留提议，由 Runner 在一致边界把 run 置为 completed
            return None

        if attempts >= MAX_FEEDBACK_ROUNDS:
            return {
                "completion_proposal": None,
                "finish_gate_feedback": [
                    "已连续多次提交不合格的结束提议，停止继续追问。"
                    "当前状态：**无法形成可靠结论**。请把未解决的具体问题直接告诉用户。"
                ],
                "work_summary": {
                    **(state.get("work_summary") or {}),
                    "unresolved_blockers": decision.reasons,
                    "blocking_objects": decision.blocking_objects,
                },
            }

        feedback = _render_feedback(decision)
        return {
            "completion_proposal": None,
            "finish_gate_feedback": decision.reasons,
            "finish_gate_attempts": attempts + 1,
            "messages": [HumanMessage(content=feedback)],
        }

    async def _handle_plain_text_finish(
        self, _state: Mapping[str, Any], attempts: int, messages: Sequence[Any]
    ) -> dict[str, Any] | None:
        """模型用普通文本收尾但没有结构化提议。

        只在**看起来像要收尾**时给出修正反馈，避免对正常中间轮次叽叽歪歪。
        """
        if not messages:
            return None
        last = messages[-1]
        if getattr(last, "tool_calls", None):
            return None  # 还在调工具，不是收尾

        text = str(getattr(last, "content", "") or "")
        if not _looks_like_finish(text):
            return None

        if attempts >= MAX_FEEDBACK_ROUNDS:
            return {
                "finish_gate_feedback": [
                    "多次尝试收尾但都没有形成结构化结束提议。"
                    "请直接用普通文本把结论、限制与未决问题讲清楚，并说明为什么无法形成提议。"
                ],
                "finish_gate_attempts": attempts + 1,
            }

        return {
            "finish_gate_attempts": attempts + 1,
            "messages": [
                HumanMessage(
                    content=(
                        "【系统提示】看起来你在收尾，但没有提交结构化结束提议。"
                        "如果要结束本阶段，请调用 `propose_completion` 并给出："
                        "结束理由、对目标的回答、已确认发现、覆盖摘要、剩余线索、停止依据、证据引用。"
                        "如果还不能结束，请继续推进下一条线索。"
                    )
                )
            ],
        }


_FINISH_MARKERS = (
    "测试完成",
    "已完成测试",
    "测试已结束",
    "没有发现问题",
    "未发现漏洞",
    "未发现安全问题",
    "报告如下",
    "testing is complete",
)
_FINISH_HEADING = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:总结|结论|综上|conclusion|summary)\s*[:：]"
)


def _looks_like_finish(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 40:
        return False
    lowered = stripped.lower()
    # 普通能力说明中的“结论/总结”不等于实际完成测试；只识别明确的收尾声明。
    return bool(_FINISH_HEADING.search(stripped)) or any(
        marker in stripped or marker in lowered for marker in _FINISH_MARKERS
    )


def _render_feedback(decision: Any) -> str:
    lines = [
        "【系统提示】结束提议未通过检查，原因如下：",
    ]
    lines.extend(f"- {reason}" for reason in decision.reasons)
    if decision.blocking_objects:
        lines.append("待处理对象：")
        for key, values in decision.blocking_objects.items():
            lines.append(f"- {key}: {values[:10]}")
    if decision.coverage_gap:
        lines.append(
            "覆盖缺口："
            f"未检查 {len(decision.coverage_gap.get('not_checked_entries') or [])} 项，"
            f"受阻 {len(decision.coverage_gap.get('blocked_entries') or [])} 项。"
        )
    lines.append(
        "请针对上述具体问题补齐；**不要**只是把线索改成 deferred 来绕过检查，"
        "也不要重复提交同一份提议。如果确实无法完成（例如缺少账号），"
        "请明确说明缺少什么条件。"
    )
    return "\n".join(lines)
