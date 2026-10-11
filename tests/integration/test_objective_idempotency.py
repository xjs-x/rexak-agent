"""方向变更重试必须复用原指令和控制版本。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import create_run
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import CommandRow, RunRow

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Iterator[TestClient]:
    from agent_workbench.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


async def test_objective_retry_does_not_bump_version_twice(
    database, unique_name, client: TestClient
) -> None:
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="objective-test",
            name=unique_name,
            target_description="幂等性演练",
            default_scope=scope.model_dump(),
        )
        task = await SessionRepository(session).create(
            project_id=project.id,
            title="方向变更",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )
        run = await create_run(
            session,
            session_id=task.id,
            objective="旧方向",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )

    csrf_response = client.post("/api/session")
    assert csrf_response.status_code == 200
    headers = {
        "X-Workbench-CSRF": csrf_response.json()["csrf_token"],
        "Idempotency-Key": f"objective-{uuid.uuid4().hex}",
    }
    url = f"/api/runs/{run.id}/objective"
    first = client.post(url, headers=headers, json={"content": "新方向"})
    second = client.post(url, headers=headers, json={"content": "新方向"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["control_version"] == 1

    conflict = client.post(url, headers=headers, json={"content": "另一方向"})
    assert conflict.status_code == 409
    async with database.session() as session:
        current = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        commands = (
            await session.execute(select(CommandRow).where(CommandRow.session_id == task.id))
        ).scalars().all()
    assert current.control_version == 1
    assert len(commands) == 1
    assert commands[0].control_version == 1
