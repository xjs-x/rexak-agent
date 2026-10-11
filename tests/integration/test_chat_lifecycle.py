"""个人工作台的创建、删除、调度边界；使用隔离的真实 PostgreSQL。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeSnapshot
from agent_workbench.domain.run_state import create_run, set_run_status
from agent_workbench.persistence.repositories import (
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import RunRow, SessionRow
from agent_workbench.runtime.recovery import recover_stale_runs
from agent_workbench.runtime.runner import AgentRunner

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("unknown_job", [False, True])
@pytest.mark.parametrize("action", ["resume", "message"])
async def test_model_timeout_can_resume_but_unknown_tools_still_require_ack(
    database, unique_name, unknown_job, action
):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=get_settings().owner_id,
            name=unique_name,
            target_description="超时恢复验收",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id, title="超时恢复", scope_snapshot={}, policy_snapshot={}
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="本地恢复验收",
            provider_id="fixture",
            model_name="scripted",
            config_snapshot={},
        )
        await set_run_status(db, run_id=run.id, target=RunStatus.RUNNING)
        await set_run_status(
            db, run_id=run.id, target=RunStatus.RECOVERING, stop_reason="ModelRequestTimeout"
        )
        if unknown_job:
            operation_id, job_id = uuid4(), uuid4()
            await ToolOperationRepository(db).prepare(
                operation_id=operation_id,
                run_id=run.id,
                session_id=task.id,
                tool_call_id="fixture-call",
                tool_name="fixture",
                request_digest="fixture",
                effect_class="read_only",
                policy_revision=1,
                scope_revision=1,
            )
            await ToolOperationRepository(db).attach_job(operation_id, job_id, status="running")
            await ToolJobRepository(db).upsert(
                job_id=job_id,
                operation_id=operation_id,
                executor_id="fixture",
                state="unknown",
                sync_status="uncertain",
            )
    with TestClient(create_app()) as client:
        csrf = client.post("/api/session").json()["csrf_token"]
        headers = {"X-Workbench-CSRF": csrf, "Idempotency-Key": str(uuid4())}
        url = (
            f"/api/runs/{run.id}/resume"
            if action == "resume"
            else f"/api/sessions/{task.id}/messages"
        )
        response = client.post(
            url, headers=headers, json={} if action == "resume" else {"content": "继续已保存的任务"}
        )
        assert response.status_code == (409 if unknown_job and action == "resume" else 202), (
            response.text
        )
        if unknown_job and action == "resume":
            assert response.json()["details"]["unresolved_jobs"]
    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        assert saved is not None
        assert saved.status == ("recovering" if unknown_job else "queued")


async def test_empty_scope_chat_creation_delete_and_cancel_without_model(database, unique_name):
    with TestClient(create_app()) as client:
        csrf = client.post("/api/session").json()["csrf_token"]
        headers = {"X-Workbench-CSRF": csrf}
        response = client.post(
            "/api/projects",
            headers=headers,
            json={
                "name": unique_name,
                "target_description": "沟通测试需求",
            },
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
        payload = {"project_id": project_id, "title": "你好", "initial_objective": "你好"}
        response = client.post(
            f"/api/projects/{project_id}/sessions", headers=headers, json=payload
        )
        assert response.status_code == 201, response.text
        task = response.json()["session"]
        run_id = response.json()["run"]["id"]
        assert task["scope"]["rules"] == []
        assert client.delete(f"/api/sessions/{task['id']}").status_code == 403
        removed = client.delete(f"/api/sessions/{task['id']}", headers=headers)
        assert removed.status_code == 200, removed.text
        assert removed.json()["cancellation_pending"] is True
        assert client.get(f"/api/projects/{project_id}/sessions").json()["sessions"] == []
        assert (
            client.post(
                f"/api/sessions/{task['id']}/messages",
                headers={
                    **headers,
                    "Idempotency-Key": str(uuid4()),
                },
                json={"content": "继续"},
            ).status_code
            == 404
        )
        assert client.delete(f"/api/sessions/{task['id']}", headers=headers).status_code == 200
    runner = AgentRunner(database=database)
    from uuid import UUID

    outcome = await runner.step(only_run_id=UUID(run_id))
    assert outcome and outcome.status is RunStatus.CANCELLED
    async with database.session() as db:
        row = (
            await db.execute(select(SessionRow).where(SessionRow.id == UUID(task["id"])))
        ).scalar_one()
        assert row.status == "deleted"
        run = (await db.execute(select(RunRow).where(RunRow.id == UUID(run_id)))).scalar_one()
        assert run.control_version == 1


async def test_production_selection_and_recovery_never_touch_other_owners(database, unique_name):
    settings = get_settings()
    runs = []
    async with database.transaction() as db:
        for index, owner in enumerate(("synthetic-other-owner", settings.owner_id)):
            project = await ProjectRepository(db).create(
                owner_id=owner,
                name=f"{unique_name}-{index}",
                target_description="队列隔离",
                default_scope=ScopeSnapshot().model_dump(),
            )
            task = await SessionRepository(db).create(
                project_id=project.id,
                title="对话",
                scope_snapshot=ScopeSnapshot().model_dump(),
                policy_snapshot=PolicySnapshot().model_dump(),
            )
            run = await create_run(
                db,
                session_id=task.id,
                objective="需求说明",
                provider_id="test",
                model_name="scripted",
                config_snapshot={"created_from": "message"},
            )
            runs.append(run)
    runner = AgentRunner(settings=settings, database=database)
    runner._production_scan = True
    candidates = await runner._candidate_runs(limit=100)
    assert runs[0].id not in {item.id for item in candidates}
    assert runs[1].id in {item.id for item in candidates}
    # 定向评测保持显式访问能力。
    assert (await runner._candidate_runs(only_run_id=runs[0].id))[0].id == runs[0].id
    async with database.transaction() as db:
        for item in runs:
            row = await db.get(RunRow, item.id)
            row.status = "running"
            row.owner = "dead-worker"
            row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    recovered = await recover_stale_runs(database, settings, owner_id=settings.owner_id)
    assert runs[0].id not in recovered
    assert runs[1].id in recovered
