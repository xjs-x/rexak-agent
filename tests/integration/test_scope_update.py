"""显式范围变更：修订幂等、旧派发拦截与工具箱刷新。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.signals import StaleResponseError
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import claim_run, create_run, set_run_status
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import CommandRow, RunRow, SessionRow
from agent_workbench.runtime.runner import AgentRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Iterator[TestClient]:
    from agent_workbench.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


async def test_scope_update_refreshes_runtime_and_rejects_old_dispatch(
    database, unique_name, client: TestClient
) -> None:
    old_scope = ScopeSnapshot(
        revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)]
    )
    new_scope = ScopeSnapshot(
        revision=2, rules=[ScopeRule(scheme="https", host="example.test", port=443)]
    )
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="scope-update-test",
            name=unique_name,
            target_description="范围修订演练",
            default_scope=old_scope.model_dump(),
        )
        task = await SessionRepository(session).create(
            project_id=project.id,
            title="范围修订",
            scope_snapshot=old_scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )
        run = await create_run(
            session,
            session_id=task.id,
            objective="测试旧范围",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )
        await set_run_status(session, run_id=run.id, target=RunStatus.RUNNING)
    async with database.transaction() as session:
        acquired, generation = await claim_run(
            session, run_id=run.id, owner="scope-test-owner", lease_seconds=120
        )
    assert acquired

    csrf = client.post("/api/session")
    assert csrf.status_code == 200
    headers = {
        "X-Workbench-CSRF": csrf.json()["csrf_token"],
        "Idempotency-Key": f"scope-{uuid.uuid4().hex}",
    }
    url = f"/api/sessions/{task.id}/scope"
    body = {"expected_revision": 1, "scope": new_scope.model_dump(), "reason": "收窄目标"}
    first = client.put(url, headers=headers, json=body)
    second = client.put(url, headers=headers, json=body)
    assert first.status_code == second.status_code == 200, first.text
    assert first.json() == second.json()
    assert first.json()["scope_revision"] == 2
    assert first.json()["control_version"] == 1
    assert client.put(url, headers=headers, json={**body, "reason": "另一理由"}).status_code == 409
    newer = {**headers, "Idempotency-Key": f"scope-{uuid.uuid4().hex}"}
    assert client.put(url, headers=newer, json=body).status_code == 409

    async with database.session() as session:
        task_row = (await session.execute(select(SessionRow).where(SessionRow.id == task.id))).scalar_one()
        run_row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        commands = (
            await session.execute(select(CommandRow).where(CommandRow.session_id == task.id))
        ).scalars().all()
    assert task_row.scope_snapshot["revision"] == 2
    assert run_row.control_version == 1
    assert len(commands) == 1

    runner = AgentRunner(owner="scope-test-owner", database=database)
    with pytest.raises(StaleResponseError):
        await runner._assert_dispatch_allowed(run.id, "scope-test-owner", generation, 0)

    toolbox = SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=task.id,
        owner="scope-test-owner",
        owner_generation=generation,
        scope=ScopePolicy(old_scope),
        scope_revision=1,
        control_version=0,
        checkpoint_id=None,
    )
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=toolbox))
    update = await CommandGuardMiddleware().abefore_model({}, runtime)
    assert toolbox.scope_revision == 2
    assert toolbox.scope.is_allowed("https://example.test/")
    assert not toolbox.scope.is_allowed("http://127.0.0.1:8899/")
    assert toolbox.control_version == 1
    assert update is not None
    assert "已显式应用" in str(update["messages"][0].content)
