"""Session graph HTTP reads preserve authoritative data and synchronization state."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.domain.session_graph import empty_graph, read_graph
from agent_workbench.persistence.repositories import SessionRepository
from agent_workbench.persistence.tables import EventRow, SessionGraphReceiptRow, SessionGraphRow

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("attack_enabled", [True, False])
@pytest.mark.parametrize("populated", [True, False])
async def test_graph_read_is_authenticated_redacted_and_read_only(
    database, monkeypatch, attack_enabled, populated
) -> None:
    monkeypatch.setattr(get_settings(), "graph_attack_enabled", attack_enabled)
    document = empty_graph()
    document["information_graph"]["nodes"] = [
        {"id": "entity-1", "label": "入口 token=visible-secret", "source_refs": []}
    ]
    document["attack_graph"]["nodes"] = [
        {"id": "state-1", "summary": "待验证", "entity_refs": ["entity-1"]}
    ]
    async with database.transaction() as db:
        task = await SessionRepository(db).create(
            project_id=None, title="Graph HTTP read", scope_snapshot={}, policy_snapshot={}
        )
        session_id = task.id
        if populated:
            db.add(
                SessionGraphRow(
                    session_id=session_id, revision=7, document=document, reviewed_seq=9,
                    completed_rounds=8, reviewed_rounds=4, next_sync_round=10, missed_syncs=1,
                )
            )

    async def snapshot():
        async with database.session() as db:
            row = (
                await db.execute(
                    select(SessionGraphRow.__table__).where(
                        SessionGraphRow.session_id == session_id
                    )
                )
            ).mappings().one_or_none()
            counts = [
                (
                    await db.execute(
                        select(func.count()).select_from(table).where(table.session_id == session_id)
                    )
                ).scalar_one()
                for table in (SessionGraphReceiptRow, EventRow)
            ]
            return (dict(row) if row else None, counts)

    before = await snapshot()
    async with database.session() as db:
        expected = await read_graph(db, session_id)
    if not attack_enabled:
        expected.pop("attack_graph")

    with TestClient(create_app()) as client:
        path = f"/api/sessions/{session_id}/graph"
        assert client.get(path).status_code == 401
        assert client.post("/api/session").status_code == 200
        assert "X-Workbench-CSRF" not in client.headers
        for _ in range(2):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.json() == expected
            assert "visible-secret" not in response.text
            assert response.json()["revision"] == (7 if populated else 0)
        assert client.get(f"/api/sessions/{uuid4()}/graph").status_code == 404

    assert await snapshot() == before


async def test_deleted_session_graph_returns_not_found(database) -> None:
    async with database.transaction() as db:
        task = await SessionRepository(db).create(
            project_id=None, title="Deleted graph", scope_snapshot={}, policy_snapshot={}
        )
        task.status = "deleted"
        session_id = task.id
        db.add(SessionGraphRow(session_id=session_id, revision=3, document=empty_graph()))

    with TestClient(create_app()) as client:
        assert client.post("/api/session").status_code == 200
        response = client.get(f"/api/sessions/{session_id}/graph")
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND


async def test_graph_owner_isolation(database, monkeypatch) -> None:
    async with database.transaction() as db:
        task = await SessionRepository(db).create(
            project_id=None, owner_id="foreign-owner", title="Foreign graph",
            scope_snapshot={}, policy_snapshot={},
        )
        session_id = task.id
        db.add(SessionGraphRow(session_id=session_id, revision=1, document=empty_graph()))
    with TestClient(create_app()) as client:
        assert client.post("/api/session").status_code == 200
        response = client.get(f"/api/sessions/{session_id}/graph")
        assert response.status_code == 404
