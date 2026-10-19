"""计划只从 LangGraph checkpoint 读取，并对浏览器输出脱敏。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.api.app import create_app
from agent_workbench.contracts.plan import AcceptanceSpec, TaskContractCreate
from agent_workbench.domain.plan_contracts import append_contract
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository

pytestmark = pytest.mark.integration


async def test_plan_endpoint_reads_authoritative_checkpoint(
    database, settings, unique_name
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="plan-read-test",
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id,
            title="计划读取",
            scope_snapshot={},
            policy_snapshot={},
        )
        other = await SessionRepository(db).create(
            project_id=project.id,
            title="其他计划",
            scope_snapshot={},
            policy_snapshot={},
        )
        session_id = task.id
        other_id = other.id
        thread_id = task.thread_id

    with TestClient(create_app()) as client:
        assert client.get(f"/api/sessions/{session_id}/plan").status_code == 401
        assert client.post("/api/session").status_code == 200
        assert client.get(f"/api/sessions/{uuid4()}/plan").status_code == 404
        assert client.get(f"/api/sessions/{session_id}/plan").json() == {
            "revision": 0,
            "current_focus": None,
            "work_assessment": None,
            "steps": [],
            "source": "langgraph_checkpoint",
            "plan_source": "legacy_plan",
            "active_contract_ids": [],
            "contracts": [],
            "frontier": [],
        }

    def noop(state: AgentGraphState) -> dict[str, int]:
        return {"plan_revision": int(state.get("plan_revision") or 0)}

    graph = StateGraph(AgentGraphState)
    graph.add_node("noop", noop)
    graph.set_entry_point("noop")
    graph.set_finish_point("noop")
    async with open_checkpointer_connection(settings) as connection:
        compiled = graph.compile(checkpointer=AsyncPostgresSaver(connection))
        await compiled.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {
                "messages": [],
                "plan_revision": 3,
                "plan": [
                    {
                        "step_id": "s1",
                        "description": "检查公开入口 token=visible-secret",
                        "status": "in_progress",
                        "rationale": "先确定入口是否存在",
                        "lead_id": "lead-1",
                    }
                ],
            },
        )

    with TestClient(create_app()) as client:
        client.post("/api/session")
        response = client.get(f"/api/sessions/{session_id}/plan")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "langgraph_checkpoint"
        assert payload["revision"] == 3
        assert payload["steps"][0]["lead_id"] == "lead-1"
        assert "visible-secret" not in response.text
        assert client.get(f"/api/sessions/{other_id}/plan").json()["steps"] == []


async def test_plan_endpoint_projects_contracts_and_frontier(
    database, settings, unique_name
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="plan-read-test",
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id,
            title="合同计划",
            scope_snapshot={},
            policy_snapshot={},
        )
        contract_id = uuid4()
        row = await append_contract(
            db,
            session_id=task.id,
            run_id=None,
            contract=TaskContractCreate(
                contract_id=str(contract_id),
                question="确认入口",
                acceptance=AcceptanceSpec(evaluator_id="test", evaluator_version="v1"),
                state="running",
                mutation_id=str(uuid4()),
            ),
        )
        thread_id = task.thread_id
        session_id = task.id

    graph = StateGraph(AgentGraphState)
    graph.add_node("noop", lambda _state: {"plan_revision": 1})
    graph.set_entry_point("noop")
    graph.set_finish_point("noop")
    async with open_checkpointer_connection(settings) as connection:
        compiled = graph.compile(checkpointer=AsyncPostgresSaver(connection))
        await compiled.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {
                "messages": [],
                "plan_revision": 1,
                "contract_refs": [
                    {
                        "contract_id": str(row.contract_id),
                        "contract_revision": row.contract_revision,
                        "content_hash": row.content_hash,
                    }
                ],
                "active_contract_ids": [str(row.contract_id)],
            },
        )

    with TestClient(create_app()) as client:
        client.post("/api/session")
        payload = client.get(f"/api/sessions/{session_id}/plan").json()
    assert payload["plan_source"] == "plan_contracts"
    assert payload["contracts"][0]["contract_id"] == str(contract_id)
    assert payload["contracts"][0]["state"] == "running"
    assert payload["frontier"][0]["contract_id"] == str(contract_id)
    assert payload["frontier"][0]["eligible"] is True
