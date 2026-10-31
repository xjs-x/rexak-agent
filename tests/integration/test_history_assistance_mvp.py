"""Production Decision Memory preserves evidence, session boundaries and pinned mode."""
from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_workbench.api.app import create_app
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import RunRow
from tests.integration.test_instruction_scope import create_task
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import final_text

pytestmark = pytest.mark.integration

def references(model):
    result = []
    for index, messages in enumerate(model.received):
        for message in messages:
            if not isinstance(message, (AIMessage, ToolMessage)):
                continue
            try:
                value = json.loads(str(message.content))
            except ValueError:
                continue
            if isinstance(value, dict) and "_host_history_reference" in value:
                result.append((index, message, value["_host_history_reference"]))
    return result


async def test_existing_unknown_history_conditions_are_low_priority_and_session_scoped(
    database, unique_name
):
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage, SystemMessage

    from agent_workbench.agent.context import RunContext
    from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
    from agent_workbench.agent.middleware.history_assistance import HistoryAssistanceMiddleware
    from agent_workbench.agent.state import AgentGraphState
    from tests.integration.test_decision_memory import seeded

    run, task, box, old_op, _, artifact = await seeded(database, unique_name)
    _, _, _, foreign_op, _, _ = await seeded(database, unique_name + "foreign")
    objective = f"核对 operation:{old_op} 的条件与未知执行状态；不要把 operation:{foreign_op} 当成本 Session 来源。"
    async with database.transaction() as db:
        row = await db.get(RunRow, run.id)
        row.objective = objective
    box.settings = box.settings.model_copy(update={"history_assistance_mode": "memory"})
    box.history_assistance_manifest = None
    model = SteppedModel(steps=[lambda _: final_text("unknown 保留；旧身份结论不沿用。")])
    async with open_checkpointer_connection(box.settings) as conn:
        saver = AsyncPostgresSaver(conn)
        graph = create_agent(
            model=model,
            tools=[],
            system_prompt="original system",
            middleware=[ContextBuilderMiddleware(), HistoryAssistanceMiddleware()],
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=saver,
        )
        config: Any = {"configurable": {"thread_id": str(task.thread_id)}}
        result = await graph.ainvoke(
            cast(Any, {"messages": [HumanMessage(content=objective, id=f"objective-{run.id}")]}),
            config,
            context=RunContext(toolbox=cast(Any, box), thread_id=task.thread_id),
        )
    refs = references(model)
    assert len(refs) == 1 and isinstance(refs[0][1], AIMessage)
    serialized = json.dumps(refs[0][2], ensure_ascii=False)
    assert str(artifact) in serialized and str(foreign_op) not in serialized
    evidence = refs[0][2]["memory"]["execution_evidence"][0]
    assert evidence["execution"]["operation_status"] == "unknown"
    changed = next(
        d for d in evidence["condition_differences"] if d["field"] == "identity_auth_revision"
    )
    assert changed["status"] == "changed" and (changed["old_value"], changed["new_value"]) == (1, 2)
    assert evidence["evidence_availability"] == "metadata_only_no_body_read"
    assert "UNTRUSTED_ARTIFACT_SENTINEL" not in serialized
    assert not any("_host_history_reference" in str(m.content) for m in result["messages"])
    assert not any(
        "_host_history_reference" in str(m.content)
        for m in model.received[0]
        if isinstance(m, SystemMessage)
    )


async def test_api_pins_new_run_mode_and_keeps_active_run_mode(database, unique_name, monkeypatch):
    from uuid import uuid4

    chosen = database.settings.model_copy(update={"history_assistance_mode": "memory"})
    monkeypatch.setattr("agent_workbench.api.routes.get_settings", lambda: chosen)
    with TestClient(create_app()) as client:
        sid, rid, headers = create_task(client, unique_name)
        chosen = chosen.model_copy(update={"history_assistance_mode": "off"})
        response = client.post(
            f"/api/sessions/{sid}/messages",
            json={"content": "继续当前阶段"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202
        _, new_rid, _ = create_task(client, unique_name + "new-off")
    async with database.session() as db:
        assert (await db.get(RunRow, rid)).config_snapshot["history_assistance_mode"] == "memory"
        assert (await db.get(RunRow, new_rid)).config_snapshot["history_assistance_mode"] == "off"
