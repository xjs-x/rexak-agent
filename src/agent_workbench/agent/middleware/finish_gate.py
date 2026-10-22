"""将显式提议或模型正常最终回复交给同一个执行一致性检查。"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, SystemMessage

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.events import EventType
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.runtime.stopping import FinishGate

log = get_logger("agent_workbench.finish_gate")


class FinishGateMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        proposal = state.get("completion_proposal")
        if not proposal or getattr(runtime, "context", None) is None:
            return None
        update = await self._review(state, runtime, dict(proposal))
        if update is None:
            result: dict[str, Any] = {"jump_to": "end"}
            if proposal.get("source") != "final_message":
                text = proposal["goal_answer"]
                if proposal.get("limitations"):
                    text += "\n\n局限与未验证项：" + str(proposal.get("limitations") or "")
                result["messages"] = [
                    AIMessage(
                        content=text,
                        id=f"completion-{runtime.context.toolbox.run_id}-{proposal.get('proposed_at_turn', 0)}",
                        additional_kwargs={"workbench_completion_result": True},
                    )
                ]
            return result
        # before_model 拒绝后按官方边继续进入模型；跳到 model 会在仅有
        # 此 before_model 节点时跳回自身，跳过正常请求或触发分支错误。
        update.pop("jump_to", None)
        return update

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        if getattr(runtime, "context", None) is None:
            return None
        proposal = state.get("completion_proposal")
        if proposal:
            return await self._review(state, runtime, dict(proposal))
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls or last.invalid_tool_calls:
            return None
        text = last.text.strip()
        if not text:
            return None
        # 官方模型无工具的最终消息就是结束信号，无需再调模型填表。
        proposal = {
            "reason": "goal_answered",
            "goal_answer": text,
            "limitations": "",
            "evidence_refs": [],
            "proposed_at_turn": len(messages),
            "source": "final_message",
        }
        update = await self._review(state, runtime, dict(proposal))
        return update if update is not None else {"completion_proposal": proposal}

    async def _review(
        self, state: Any, runtime: Any, proposal: dict[str, Any]
    ) -> dict[str, Any] | None:
        toolbox = runtime.context.toolbox
        span = toolbox.spans.start(
            "finish_gate.middleware", session_id=toolbox.session_id, run_id=toolbox.run_id
        )
        decision = None
        try:
            async with toolbox.db.session() as db:
                decision = await FinishGate(db).evaluate(
                    session_id=toolbox.session_id, proposal=proposal, run_id=toolbox.run_id
                )
            span.finish()
            toolbox.spans.record(span, **decision.as_metadata())
        except Exception as exc:
            span.finish(error=type(exc).__name__)
            raise
        finally:
            try:
                async with toolbox.db.transaction() as db:
                    await toolbox.spans.flush(db)
                    if decision is not None:
                        await append_event(
                            db,
                            session_id=toolbox.session_id,
                            run_id=toolbox.run_id,
                            event_type=EventType.COMPLETION_REVIEW,
                            payload=decision.as_metadata(),
                        )
            except Exception as exc:
                log.warning(
                    "finish_gate_span_persist_failed",
                    run_id=str(toolbox.run_id),
                    error_type=type(exc).__name__,
                )
        if decision is None:
            raise RuntimeError("结束检查没有返回决策")
        if decision.allowed or decision.decision in {"wait_user", "wait_tool"}:
            return None
        messages = state.get("messages") or []
        return {
            "completion_proposal": None,
            "finish_gate_feedback": decision.reasons,
            "finish_gate_attempts": int(state.get("finish_gate_attempts") or 0) + 1,
            "messages": [
                SystemMessage(
                    content="结束一致性检查："
                    + "; ".join(decision.reasons)
                    + "。请处理指令或修正引用；不要求补齐计划、假设或覆盖记录。"
                )
            ],
            **(
                {"jump_to": "model"}
                if messages and not getattr(messages[-1], "tool_calls", None)
                else {}
            ),
        }
