"""`resume` 对 `recovering` 的 run 必须要求显式确认。

设计意图：处于 `recovering` 说明**启动核对没能确认远端副作用**。
这种情况绝不能被一次普通的 resume 悄悄盖过去 —— 必须是"用户看过未决任务清单之后
主动确认"，否则就等于用 API 调用把不确定性抹掉。

这里通过真实 HTTP 层（TestClient）验证，而不是只测领域函数：
鉴权、CSRF、错误体、状态码都属于契约的一部分。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import create_run, set_run_status
from agent_workbench.persistence.repositories import (
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import RunRow

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Iterator[TestClient]:
    from agent_workbench.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _csrf(client: TestClient) -> str:
    response = client.post("/api/session")
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


async def _recovering_run_with_unknown_job(database, name: str):
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="resume-owner",
            name=name,
            target_description="resume 测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="resume",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
        await set_run_status(session, run_id=run.id, target=RunStatus.RUNNING)

    # 伪造"Runner 崩了 + 有一个状态未知的远端任务"
    async with database.transaction() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        row.owner = "runner-that-died"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=120)

    operation_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with database.transaction() as session:
        await ToolOperationRepository(session).prepare(
            operation_id=operation_id,
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id=f"call-{uuid.uuid4().hex[:6]}",
            tool_name="http_request",
            request_digest="d",
            effect_class="read_only",
            policy_revision=1,
            scope_revision=1,
        )
        await ToolOperationRepository(session).attach_job(operation_id, job_id, status="running")
    async with database.transaction() as session:
        await ToolJobRepository(session).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id="kali-01",
            state="unknown",
            sync_status="uncertain",
        )
    return run, job_id


async def test_resume_recovering_requires_acknowledgement(
    database, unique_name, client: TestClient
) -> None:
    from agent_workbench.runtime.recovery import recover_stale_runs

    run, job_id = await _recovering_run_with_unknown_job(database, unique_name)
    await recover_stale_runs(database)

    csrf = _csrf(client)
    headers = {"X-Workbench-CSRF": csrf, "Idempotency-Key": f"resume-{uuid.uuid4().hex}"}

    # 不带确认 -> 409，并给出未决任务清单与所需字段
    response = client.post(f"/api/runs/{run.id}/resume", headers=headers, json={})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "conflict"
    assert "acknowledge_unknown_operations" in body["message"]
    assert body["details"]["status"] == str(RunStatus.RECOVERING)
    assert body["details"]["required_field"] == "acknowledge_unknown_operations"
    assert any(str(job_id) in item for item in body["details"]["unresolved_jobs"])

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.RECOVERING, "被拒绝的 resume 不得改变状态"

    # 带确认 -> 202，进入 queued
    response = client.post(
        f"/api/runs/{run.id}/resume",
        headers=headers,
        json={"acknowledge_unknown_operations": True},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == str(RunStatus.QUEUED)
    assert payload["acknowledged_unknown_operations"] is True

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.QUEUED


async def test_resume_completed_run_is_rejected(database, unique_name, client: TestClient) -> None:
    """已完成阶段必须走"新 run"，不能 resume（§4.2）。"""
    run, _job_id = await _recovering_run_with_unknown_job(database, unique_name)
    async with database.transaction() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        row.status = str(RunStatus.COMPLETED)
        row.owner = None
        row.lease_until = None

    csrf = _csrf(client)
    response = client.post(
        f"/api/runs/{run.id}/resume",
        headers={"X-Workbench-CSRF": csrf, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"acknowledge_unknown_operations": True},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "run_not_resumable"


def test_resume_requires_auth_and_csrf(client: TestClient) -> None:
    csrf = _csrf(client)
    run_id = uuid.uuid4()
    # 没有 CSRF -> 403
    response = client.post(
        f"/api/runs/{run_id}/resume",
        headers={"Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={},
    )
    assert response.status_code == 403
    # 有 CSRF 但 run 不存在 -> 404
    response = client.post(
        f"/api/runs/{run_id}/resume",
        headers={"X-Workbench-CSRF": csrf, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={},
    )
    assert response.status_code == 404
