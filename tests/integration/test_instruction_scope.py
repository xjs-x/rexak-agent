"""用户目标无需范围表单；真实数据库验证修订、来源与运行时刷新。"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import CommandKind, RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.domain.instruction_scope import sync_instruction_scope
from agent_workbench.domain.run_state import claim_run, set_run_status
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import CommandRepository
from agent_workbench.persistence.tables import RunRow, SessionRow
from agent_workbench.runtime.runner import AgentRunner

pytestmark = pytest.mark.integration


def create_task(client, name, *, objective="你好", scope=None):
    headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
    response = client.post(
        "/api/projects", headers=headers, json={"name": name, "target_description": "指令模式验收"}
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    body = {"project_id": project_id, "title": name, "initial_objective": objective}
    if scope is not None:
        body["scope"] = scope
    response = client.post(f"/api/projects/{project_id}/sessions", headers=headers, json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    return UUID(data["session"]["id"]), UUID(data["run"]["id"]), headers


async def test_instruction_message_updates_scope_once_and_refreshes_runtime(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, headers = create_task(
            client, unique_name, objective="测试 http://lab.example.test/"
        )
        async with database.transaction() as db:
            task = await db.get(SessionRow, task_id)
            initial = ScopeSnapshot.model_validate(task.scope_snapshot)
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
            acquired, generation = await claim_run(
                db, run_id=run_id, owner="instruction-test", lease_seconds=120
            )
        assert acquired
        assert initial.mode == "instruction"
        assert ScopePolicy(initial).is_allowed("tcp://lab.example.test:22/")
        toolbox = SimpleNamespace(
            db=database,
            run_id=run_id,
            session_id=task_id,
            owner="instruction-test",
            owner_generation=generation,
            scope=ScopePolicy(initial),
            scope_revision=1,
            control_version=0,
            checkpoint_id=None,
        )
        headers["Idempotency-Key"] = str(uuid4())
        for _ in range(2):
            response = client.post(
                f"/api/sessions/{task_id}/messages",
                headers=headers,
                json={"content": "接着测试 https://second.example.test:8443/"},
            )
            assert response.status_code == 202, response.text
        async with database.session() as db:
            task = await db.get(SessionRow, task_id)
            run = await db.get(RunRow, run_id)
            assert task.scope_snapshot["revision"] == 2
            assert run.control_version == 1
        update = await CommandGuardMiddleware().abefore_model(
            {"completion_proposal": {"reason": "goal_answered", "goal_answer": "此前结果"}},
            SimpleNamespace(context=SimpleNamespace(toolbox=toolbox)),
        )
        assert update is not None
        assert "completion_proposal" not in update, "退役提议不再被新循环写入或读取"
        assert toolbox.scope_revision == 2 and toolbox.control_version == 1
        assert toolbox.scope.is_allowed("http://second.example.test:8080/")
        assert not toolbox.scope.is_allowed("http://third.example.test/")
        assert update is not None
        assert "second.example.test" in str(update["messages"][0].content)


async def test_strict_mode_does_not_auto_expand(database, unique_name):
    scope = {
        "mode": "scoped",
        "rules": [{"scheme": "https", "host": "strict.example.test", "port": 443}],
    }
    with TestClient(create_app()) as client:
        task_id, _, headers = create_task(
            client, unique_name, objective="测试 http://other.example.test/", scope=scope
        )
        headers["Idempotency-Key"] = str(uuid4())
        assert (
            client.post(
                f"/api/sessions/{task_id}/messages",
                headers=headers,
                json={"content": "测试 http://third.example.test/"},
            ).status_code
            == 202
        )
    async with database.session() as db:
        task = await db.get(SessionRow, task_id)
        policy = ScopePolicy(ScopeSnapshot.model_validate(task.scope_snapshot))
    assert policy.is_allowed("https://strict.example.test/")
    assert not policy.is_allowed("http://strict.example.test/")
    assert not policy.is_allowed("http://other.example.test/")


async def test_legacy_task_restores_only_user_targets_and_resume_content(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(
            client, unique_name, objective="测试 http://legacy.example.test/"
        )
    async with database.transaction() as db:
        task = await db.get(SessionRow, task_id)
        task.scope_snapshot = {"revision": 1, "rules": []}  # 旧版持久化快照没有 mode。
        await append_event(
            db,
            session_id=task_id,
            run_id=run_id,
            event_type=EventType.MESSAGE_COMPLETED,
            payload={"role": "assistant", "content": "测试 http://model.example.test/"},
        )
        await CommandRepository(db).submit(
            session_id=task_id,
            run_id=run_id,
            kind=CommandKind.RESUME,
            content="",
            idempotency_key=str(uuid4()),
            payload={"content": "继续测试 http://resume.example.test/"},
            priority=5,
            control_version=0,
        )
    async with database.transaction() as db:
        scope = await sync_instruction_scope(db, session_id=task_id, run_id=run_id)
    assert scope.mode == "instruction"
    policy = ScopePolicy(scope)
    assert policy.is_allowed("http://legacy.example.test/")
    assert policy.is_allowed("tcp://resume.example.test:22/")
    assert not policy.is_allowed("http://model.example.test/")
    async with database.transaction() as db:
        assert await sync_instruction_scope(db, session_id=task_id, run_id=run_id) == scope
        await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
        _, generation = await claim_run(db, run_id=run_id, owner="resume-test", lease_seconds=120)
    toolbox = SimpleNamespace(
        db=database,
        run_id=run_id,
        session_id=task_id,
        owner="resume-test",
        owner_generation=generation,
        scope=policy,
        scope_revision=scope.revision,
        control_version=1,
        checkpoint_id=None,
    )
    update = await CommandGuardMiddleware().abefore_model(
        {}, SimpleNamespace(context=SimpleNamespace(toolbox=toolbox))
    )
    assert update is not None
    assert "resume.example.test" in str(update["messages"][0].content)


async def test_cancel_finalization_from_running_uses_legal_transition(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
    async with database.transaction() as db:
        await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
        await CommandRepository(db).submit(
            session_id=task_id,
            run_id=run_id,
            kind=CommandKind.CANCEL,
            content="",
            idempotency_key=str(uuid4()),
            payload={},
            priority=10,
            control_version=0,
        )
    await AgentRunner(database=database)._finalize(
        run_id, RunStatus.CANCELLED, detail="取消已核对", completion_reason=None
    )
    async with database.session() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
        assert run.status == "cancelled"
        assert await CommandRepository(db).list_pending(task_id) == []
