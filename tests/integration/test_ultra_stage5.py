"""Synthetic CTF closure through native Main/Pi, HTTP tool channel and isolated Executor ledger."""
import json
import os
import shutil

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select, text

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.collaboration import CollaborationMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.delegation import DelegationInput
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.tables import AgentDelegationRow
from agent_workbench.tools.delegation import DELEGATION_TOOLS
from tests.integration.test_ultra_collaboration import collaboration_runtime, toolbox  # noqa: F401
from tests.integration.test_ultra_delegations import make_manager
from tests.integration.test_worker_evidence import artifact

pytestmark = pytest.mark.integration


async def test_synthetic_ctf_full_collaboration_and_recap(database, collaboration_runtime, tmp_path):  # noqa: F811
    cfg = collaboration_runtime
    manager = await make_manager(database, cfg)
    source = await artifact(database, cfg, manager.session_id, b"Synthetic CTF target: combine prior output and Main clue")

    class Main(BaseChatModel):
        @property
        def _llm_type(self):
            return "controlled-stage5-main"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            outputs = [m for m in messages if isinstance(m, ToolMessage)]
            parsed = [json.loads(str(m.content)) for m in outputs]
            calls = len(outputs)
            assert not any(p.get("state") in {"failed", "unknown", "rejected"} for p in parsed), [{k: p.get(k) for k in ("state", "summary", "diagnostic")} for p in parsed]
            if calls < 2:
                name, args = "delegate_task", {"objective": f"Explore synthetic route {calls}", "context_refs": [str(source)]}
            elif calls == 2:
                name, args = "wait_delegated_task", {"delegation_id": parsed[0]["id"], "seconds": 10}
            elif calls == 3:
                assert any("stage4-important-hint" in str(m.content) for m in messages if str(m.id).startswith("worker-finding:"))
                name, args = "send_delegated_message", {"delegation_id": parsed[0]["id"], "message": "stage4-main-clue"}
            elif sum(m.name == "delegate_task" for m in outputs) == 2:
                if outputs[-1].name == "wait_delegated_task" and parsed[-1]["state"] == "completed":
                    assert "clue=stage4-main-clue" in parsed[-1]["summary"]
                    name, args = "delegate_task", {"objective": "Recover synthetic goal from prior tools and clue", "resume_from_delegation_id": parsed[0]["id"]}
                else:
                    name, args = "wait_delegated_task", {"delegation_id": parsed[0]["id"], "seconds": 10}
            elif not any(m.name == "cancel_delegated_task" for m in outputs):
                if outputs[-1].name == "wait_delegated_task" and parsed[-1]["state"] == "completed":
                    assert "synthetic-goal-result: recovered-clue" in parsed[-1]["summary"]
                    name, args = "cancel_delegated_task", {"delegation_id": parsed[1]["id"]}
                else:
                    latest = next(p for m, p in reversed(list(zip(outputs, parsed, strict=True))) if m.name == "delegate_task")
                    name, args = "wait_delegated_task", {"delegation_id": latest["id"], "seconds": 10}
            else:
                completed = next(p for p in reversed(parsed[:-1]) if "synthetic-goal-result" in p.get("summary", ""))
                findings = [json.loads(str(m.content).split("\n", 1)[1]) for m in messages if str(m.id).startswith("worker-finding:")]
                references = [ref for finding in findings for ref in finding.get("output_refs", [])]
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps({
                    "result": completed["summary"], "operation_ids": [r["operation_id"] for r in references],
                    "execution_evidence": references, "route": "Worker bash -> read_evidence -> report_finding; Main sent stage4-main-clue; new Worker reused actual tool history",
                    "main": "Selected continuation and requested cancellation through the existing tool", "worker": "Executed tools and reported candidate",
                    "evidence_artifact": str(source), "result_artifact": completed["result_artifact_id"],
                    "stop_state": parsed[-1]["state"],
                    "unconfirmed_stop": parsed[-1]["id"] if parsed[-1]["state"] == "cancel_requested" else None, "uncertainty": "Controlled models; no external platform verification",
                    "executor": "isolated real Linux" if os.environ.get("PI_REAL_EXECUTOR") == "true" else "Fake Executor"
                })))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{"id": f"ctf-{calls}", "name": name, "args": args}]))])

    graph = create_agent(Main(), tools=DELEGATION_TOOLS, state_schema=AgentGraphState,
        context_schema=RunContext, middleware=[CollaborationMiddleware()])
    result = await graph.ainvoke({"messages": [HumanMessage(content="Solve synthetic target and recap with evidence")]},
        {"recursion_limit": 150}, context=RunContext(toolbox=toolbox(database, cfg, manager)))
    recap = json.loads(result["messages"][-1].content)
    assert "historical-tool-output=true" in recap["result"]
    assert recap["stop_state"] in {"cancelled", "cancel_requested", "completed"}
    async with database.session() as db:
        rows = list((await db.scalars(select(AgentDelegationRow).where(AgentDelegationRow.parent_run_id == manager.run_id))).all())
        assert len(rows) == 3
        assert len({r.a2a_task_id for r in rows}) == len({r.context_id for r in rows}) == len({r.worker_id for r in rows}) == 3
        resumed = next(r for r in rows if r.handoff_snapshot.get("resume_from_delegation_id"))
        prior = next(r for r in rows if str(r.id) == resumed.handoff_snapshot["resume_from_delegation_id"])
        assert prior.state == resumed.state == "completed"
        history = (await db.execute(text("SELECT transcript FROM ultra_worker.histories WHERE request_id=:id"), {"id": prior.id})).scalar_one()
        assert any(m["role"] == "toolResult" for m in history["messages"])
        assert "thinking" not in json.dumps(history)
        operations = (await db.execute(text("SELECT operation_id FROM app.tool_operations WHERE run_id=:id"), {"id": manager.run_id})).scalars().all()
        assert operations and recap["operation_ids"]
        assert set(recap["operation_ids"]).issubset({str(i) for i in operations})
        recap["source_transcript"] = history["messages"]
    destination = tmp_path / "synthetic-ctf-recap.json"
    destination.write_text(json.dumps(recap, indent=2), encoding="utf-8")
    if target := os.environ.get("REXAK_CTF_RECAP"):
        shutil.copyfile(destination, target)


@pytest.mark.parametrize("kind", ["missing", "damaged", "cross_session", "incomplete"])
async def test_resume_rejects_unsafe_history(database, collaboration_runtime, kind):  # noqa: F811
    cfg = collaboration_runtime
    manager = await make_manager(database, cfg)
    source = await artifact(database, cfg, manager.session_id, b"history test")
    first = await manager.delegate("source", DelegationInput(objective="Explore", context_refs=[source]), owner="test-owner", generation=1, control_version=0)
    first = await manager.wait(first.id, 10)
    assert first.state == "completed"
    async with database.transaction() as db:
        if kind == "missing":
            await db.execute(text("DELETE FROM ultra_worker.histories WHERE request_id=:id"), {"id": first.id})
        elif kind == "damaged":
            await db.execute(text("UPDATE ultra_worker.histories SET sha256=:hash WHERE request_id=:id"), {"id": first.id, "hash": "0" * 64})
        elif kind == "incomplete":
            row = await db.get(AgentDelegationRow, first.id)
            row.state = "unknown"
    target = await make_manager(database, cfg) if kind == "cross_session" else manager
    value = DelegationInput(objective="Continue", resume_from_delegation_id=first.id)
    if kind in {"cross_session", "incomplete"}:
        with pytest.raises(ApiError):
            await target.delegate("resume", value, owner="test-owner", generation=1, control_version=0)
    else:
        second = await target.delegate("resume", value, owner="test-owner", generation=1, control_version=0)
        second = await target.wait(second.id, 10)
        assert second.state == "failed" and "context was not resumed" in second.summary
        async with database.session() as db:
            assert await db.scalar(text("SELECT count(*) FROM ultra_worker.model_calls WHERE request_id=:id"), {"id": second.id}) == 0
