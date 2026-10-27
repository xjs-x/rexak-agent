"""官方 create_agent 上的独立离线提示实验；脚本响应不是 LLM 收益。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from experiments.history_action_autonomous_shadow_v1.fixture import (
    make_box,
    policy_for,
    save_policy,
)
from experiments.history_action_reflection_preparation_v1.benchmark import ROOT, write
from experiments.history_action_reflection_preparation_v1.reflection import (
    FeedbackLedger,
    all_tools_paired,
    notice,
)
from experiments.history_action_trusted_shadow_v1.fixture import parsed, tool_arguments
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from tests.support.scripted_model import ScriptedChatModel

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.tables import RunRow
from agent_workbench.tools.shadow_test_profile import run_http_test_profile


class Model(ScriptedChatModel):
    inputs: list = Field(default_factory=list)
    react: bool = False
    next_profile: str = "baseline"

    def _generate(self, messages, *args, **kwargs):
        self.inputs.append([m.model_dump(mode="json") for m in messages])
        done = sum(isinstance(m, ToolMessage) for m in messages)
        hinted = any(
            getattr(m, "id", "") and str(m.id).startswith("passive-reflection:") for m in messages
        )
        final = done >= 3 or (done >= 2 and hinted and self.react)
        self.responses = [
            AIMessage(content="scripted final; no real model efficacy")
            if final
            else AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_http_test_profile",
                        "args": {"profile_id": "baseline" if done == 0 else self.next_profile},
                        "id": f"call-{done}",
                        "type": "tool_call",
                    }
                ],
            )
        ]
        return super()._generate(messages, *args, **kwargs)


class PassiveMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(self, box: Any, arm: str, ledger: FeedbackLedger) -> None:
        self.box, self.arm, self.ledger = box, arm, ledger
        self.injected: list[dict] = []
        self.errors: list[str] = []

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)  # 原调用已经发生；原 ToolMessage 原样返回。
        try:
            service = self.box.history_action_shadow
            if service:
                await service.drain()  # 仅确定性实验屏障，生产不可据此等待观察器。
                if self.arm == "C":
                    for outcome in service.outcomes:
                        payload = notice(outcome)
                        if payload:
                            self.ledger.enqueue(payload)
        except Exception as exc:
            self.errors.append(type(exc).__name__)
        return result

    async def awrap_model_call(self, request, handler):
        if self.arm == "C" and all_tools_paired(request.messages):
            try:
                note = self.ledger.claim_next()
            except Exception as exc:
                self.errors.append(type(exc).__name__)
                note = None
            if note is not None:
                self.injected.append(
                    {
                        "after_tool_results": sum(
                            isinstance(m, ToolMessage) for m in request.messages
                        ),
                        "message": note.model_dump(mode="json"),
                    }
                )
                return await handler(request.override(messages=[*request.messages, note]))
        return await handler(request)


async def run(root: Path, arm: str, react: bool, *, recreate: bool = False, task_id: str | None = None) -> dict:
    if arm not in {"A", "B", "C"}:
        raise ValueError("unknown_experimental_arm")
    profile = parsed(tool_arguments()).model_dump(exclude={"note"})
    profiles = None
    task_text = "fixed offline task"
    if task_id is not None:
        tasks = json.loads((ROOT / "prepared/tasks.json").read_text(encoding="utf-8"))
        task = next(t for t in tasks if t["task_id"] == task_id)
        if task_id not in {"A", "B", "D", "H"}:
            raise ValueError("task_loader_not_validated_no_live_fallback")
        task_text = task["user_task"]
        profiles = {"baseline": {"request": profile}, "next": {"request": profile}}
        if task_id == "B":
            profiles["next"]["retest_required"] = True
        if task_id == "D":
            profiles["next"]["configuration"] = "fixture-configuration/2"
    box = make_box(root, arm != "A", profiles)
    box.run_id, box.session_id = uuid4(), uuid4()
    row = box.db.rows[RunRow][0]
    row.id, row.session_id = box.run_id, box.session_id
    save_policy(box, policy_for(box, profiles))
    thread = str(uuid4())
    ledger = FeedbackLedger(root / "feedback", str(box.run_id), thread)
    saver = InMemorySaver()
    context = RunContext(toolbox=box, thread_id=thread)
    config: Any = {"configurable": {"thread_id": thread}, "recursion_limit": 16}
    model = Model(react=react, next_profile="next" if task_id else "baseline")
    middleware = PassiveMiddleware(box, arm, ledger)

    def graph():
        return create_agent(
            model=model,
            tools=[run_http_test_profile],
            middleware=[ToolDispatchMiddleware(), middleware],
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=saver,
            interrupt_after=["tools"] if recreate else None,
        )

    runner = graph()
    try:
        result = await runner.ainvoke(
            cast(
                Any,
                {
                    "messages": [HumanMessage(content=task_text, id="task")],
                    "dispatch_control_version": 0,
                },
            ),
            config,
            context=context,
        )
        injections = []
        if recreate:
            for _ in range(5):
                state = await runner.aget_state(config)
                if not state.next:
                    break
                injections.extend(middleware.injected)
                middleware = PassiveMiddleware(
                    box, arm, FeedbackLedger(root / "feedback", str(box.run_id), thread)
                )
                runner = graph()  # 新图/新 middleware，复用真实 checkpoint 状态与磁盘收据。
                result = await runner.ainvoke(None, config, context=context)
        injections.extend(middleware.injected)
        if box.history_action_shadow:
            await box.history_action_shadow.drain()
        assert all_tools_paired(result["messages"])
        calls = [c for m in result["messages"] if isinstance(m, AIMessage) for c in m.tool_calls]
        tools = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert all(m.status != "error" for m in tools)
        assert len(box.executor.jobs) == len(calls)
        return {
            "arm": arm,
            "task_id": task_id,
            "react_script": react,
            "recreated": recreate,
            "identity": {"run": str(box.run_id), "thread": thread},
            "injections": injections,
            "model_requests": len(model.inputs),
            "tool_calls": calls,
            "tool_messages": [m.model_dump(mode="json") for m in tools],
            "physical_mock_jobs": len(box.executor.jobs),
            "model_inputs": model.inputs,
            "final": result["messages"][-1].content,
            "real_usage": "NOT_AVAILABLE_SCRIPTED_MODEL",
            "provider_requests": 0,
            "first_triggering_call_saved": False,
        }
    finally:
        if box.history_action_shadow:
            await box.history_action_shadow.aclose()
        await box.executor.close()


async def main(pilot_tasks: bool = False):
    rows = []
    if pilot_tasks:
        for task_id in ("A", "B", "D", "H"):
            for arm in ("A", "B", "C"):
                rows.append(await run(ROOT / "task-link-data" / f"{task_id}-{arm}", arm,
                                      task_id == "A", task_id=task_id))
        write(ROOT / "task-link-results.json", {"rows": rows, "claim": "SCRIPTED_TASK_LOADER_PROTOCOL_ONLY"})
        return
    for arm in ("A", "B", "C"):
        for react in (False, True):
            rows.append(await run(ROOT / "offline-v2-data" / f"{arm}-{react}", arm, react))
    rows.append(await run(ROOT / "offline-v2-data/recovery", "C", False, recreate=True))
    write(
        ROOT / "offline-results-v2.json", {"rows": rows, "claim": "PROTOCOL_ONLY_NOT_LLM_BENEFIT"}
    )


if __name__ == "__main__":
    asyncio.run(main("--pilot-tasks" in sys.argv))
