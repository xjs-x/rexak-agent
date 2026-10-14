"""弱点、关系与计划读接口：同一实体、按需展开、会话隔离。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.weaknesses import WeaknessRelationRef
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.weakness_relations import upsert_links
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import SessionRow

pytestmark = pytest.mark.integration


@contextmanager
def api_client() -> Iterator[TestClient]:
    """已建立本地会话的客户端；未建立会话时读接口返回 401。"""
    with TestClient(create_app()) as client:
        assert client.post("/api/session").status_code == 200
        yield client


async def _session(database, unique_name, title="弱点读接口"):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=get_settings().owner_id,
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id, title=title, scope_snapshot={}, policy_snapshot={}
        )
        return session.id


async def test_weakness_and_relation_read_endpoints(database, unique_name) -> None:
    session_id = await _session(database, unique_name)
    async with database.transaction() as db:
        findings = FindingService(db)
        weakness = await findings.upsert_weakness(
            session_id=session_id,
            title="错误页回显路径",
            description="响应体含绝对路径",
            weakness_kind="enabling_condition",
            applicability={"limitations": ["仅 debug"]},
            provided_conditions=[{"statement": "错误页包含绝对路径"}],
        )
        lead = await LeadService(db).upsert(
            session_id=session_id, hypothesis="可做目录穿越", next_action="用 ../ 探测"
        )
        await upsert_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id),
                    role="basis",
                    condition_key=weakness.condition_keys[0],
                    source_weakness_revision=weakness.revision,
                )
            ],
        )
        await findings.upsert_weakness(
            session_id=session_id,
            weakness_id=weakness.weakness_id,
            expected_revision=weakness.revision,
            connection_review={"linked_lead_ids": [str(lead.id)]},
        )

    with api_client() as client:
        listing = client.get(f"/api/sessions/{session_id}/weaknesses")
        assert listing.status_code == 200
        body = listing.json()
        assert [item["weakness_kind"] for item in body["weaknesses"]] == ["enabling_condition"]
        assert body["weaknesses"][0]["connection_reviewed"] is True
        assert "不等于漏洞" in body["note"]

        detail = client.get(f"/api/sessions/{session_id}/weaknesses/{weakness.weakness_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["weakness"]["provided_conditions"][0]["statement"] == "错误页包含绝对路径"
        assert payload["relations"][0]["role"] == "basis"
        assert payload["related_leads"][0]["id"] == str(lead.id)
        assert payload["weakness"]["connection_review_stale"] is False

        relations = client.get(
            f"/api/sessions/{session_id}/exploration-relations",
            params={"weakness_id": str(weakness.weakness_id)},
        )
        assert relations.status_code == 200
        assert relations.json()["relations"][0]["lead_id"] == str(lead.id)
        assert relations.json()["next_cursor"] is None

        # 旧接口仍返回同一实体，并带上弱点角色。
        legacy = client.get(f"/api/sessions/{session_id}/findings")
        assert legacy.status_code == 200
        assert legacy.json()["findings"][0]["id"] == str(weakness.weakness_id)
        assert legacy.json()["findings"][0]["weakness_kind"] == "enabling_condition"


async def test_weakness_read_api_is_session_scoped(database, unique_name) -> None:
    first = await _session(database, unique_name)
    second = await _session(database, unique_name + "-b", title="另一个会话")
    async with database.transaction() as db:
        created = await FindingService(db).upsert_weakness(
            session_id=first, title="只属于第一个会话"
        )
    with api_client() as client:
        assert (
            client.get(f"/api/sessions/{second}/weaknesses/{created.weakness_id}").status_code
            == 404
        )
        assert client.get(f"/api/sessions/{first}/weaknesses").json()["weaknesses"], "本会话可见"
        assert client.get(f"/api/sessions/{second}/weaknesses").json()["weaknesses"] == []


async def test_weakness_read_requires_local_session(database, unique_name) -> None:
    session_id = await _session(database, unique_name, title="未登录读取")
    anonymous = TestClient(create_app())
    try:
        assert anonymous.get(f"/api/sessions/{session_id}/weaknesses").status_code == 401
        assert anonymous.get(f"/api/sessions/{session_id}/exploration-relations").status_code == 401
    finally:
        anonymous.close()


async def test_relation_pagination_is_stable(database, unique_name) -> None:
    session_id = await _session(database, unique_name)
    async with database.transaction() as db:
        findings = FindingService(db)
        lead = await LeadService(db).upsert(
            session_id=session_id, hypothesis="多条关系", next_action="逐条核对"
        )
        for index in range(3):
            created = await findings.upsert_weakness(session_id=session_id, title=f"弱点 {index}")
            await upsert_links(
                db,
                session_id=session_id,
                lead_id=lead.id,
                refs=[
                    WeaknessRelationRef(
                        weakness_id=str(created.weakness_id),
                        role="basis",
                        source_weakness_revision=created.revision,
                    )
                ],
            )
    with api_client() as client:
        first = client.get(
            f"/api/sessions/{session_id}/exploration-relations", params={"limit": 2}
        ).json()
        assert len(first["relations"]) == 2 and first["next_cursor"]
        second = client.get(
            f"/api/sessions/{session_id}/exploration-relations",
            params={"limit": 2, "cursor": first["next_cursor"]},
        ).json()
        assert len(second["relations"]) == 1 and second["next_cursor"] is None
        seen = {item["link_id"] for item in first["relations"]} | {
            item["link_id"] for item in second["relations"]
        }
        assert len(seen) == 3


async def test_plan_read_maps_legacy_lead_id_to_lead_ids(database, unique_name) -> None:
    from langchain.agents import create_agent
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.agent.state import AgentGraphState
    from tests.support.scripted_model import ScriptedChatModel, final_text

    session_id = await _session(database, unique_name, title="计划读接口")
    async with database.session() as db:
        session = (
            await db.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one()
        thread_id = session.thread_id
    lead_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    async with open_checkpointer_connection(get_settings()) as connection:
        graph = create_agent(
            model=ScriptedChatModel(responses=[final_text("本地写入")]),
            tools=[],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(connection),
        )
        # 旧步骤只有 lead_id：读取时映射为 lead_ids，不回写历史。
        await graph.aupdate_state(
            cast(Any, config),
            {
                "plan_revision": 2,
                "plan": [
                    {
                        "step_id": "s1",
                        "description": "旧步骤",
                        "status": "in_progress",
                        "lead_id": lead_id,
                    }
                ],
            },
        )
    with api_client() as client:
        response = client.get(f"/api/sessions/{session_id}/plan")
    assert response.status_code == 200
    step = response.json()["steps"][0]
    assert step["lead_ids"] == [lead_id]
    assert step["lead_id"] == lead_id
    assert step["kind"] == "mixed", "没有依据的旧步骤按兼容默认读取"
    assert UUID(step["lead_id"]) == UUID(lead_id)
