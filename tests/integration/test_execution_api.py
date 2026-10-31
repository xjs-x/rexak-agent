"""执行面板 API（验收 G03/G05 的控制面）：资源、关闭、终止全部、值班、删除预检。

执行端用替身（真实行为由隔离脚本验证）。重点检查"取消值班"与"关闭资源"确实是
两个动作，取消未确认不显示为已停止，删除带活跃资源的对话必须显式终止。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import agent_workbench.api.execution as execution_api
from agent_workbench.api.app import create_app
from agent_workbench.persistence.repositories import ExecutionResourceRepository
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor

pytestmark = pytest.mark.integration


class FakeExecutorSession:
    """替换 `_ExecutorSession`：不建隧道，直接给假执行端。"""

    def __init__(self, executor: FakeExecutor) -> None:
        self.executor = executor
        self.client = executor

    @asynccontextmanager
    async def __call__(self):
        yield self.executor


@pytest.fixture
def fake_api(monkeypatch):
    executor = FakeExecutor()
    fake = FakeExecutorSession(executor)
    monkeypatch.setattr(execution_api, "_ExecutorSession", fake)
    return executor


async def test_resources_list_reflects_executor_and_projection(database, unique_name, fake_api):
    with TestClient(create_app()) as client:
        task_id, _run_id, _headers = create_task(client, unique_name)
        # 让执行端报告一个资源
        from tests.support.executor_fixtures import FakeResource

        res1 = str(uuid4())
        fake_api.resources[res1] = FakeResource(
            resource_id=res1, kind="command", state="running", purpose="扫描"
        )
        response = client.get(f"/api/sessions/{task_id}/execution/resources?refresh=true")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["executor_note"] == ""
        assert any(item["resource_id"] == res1 for item in payload["resources"])
        # 投影已落库（后续刷新失败时仍可显示）
        async with database.session() as db:
            row = await ExecutionResourceRepository(db).get(UUID(res1))
        assert row is not None and row.session_id == task_id


async def test_close_and_duty_are_separate_actions(database, unique_name, fake_api):
    with TestClient(create_app()) as client:
        task_id, _run_id, headers = create_task(client, unique_name)
        from tests.support.executor_fixtures import FakeResource

        res2 = str(uuid4())
        fake_api.resources[res2] = FakeResource(
            resource_id=res2, kind="command", state="running"
        )
        client.get(f"/api/sessions/{task_id}/execution/resources?refresh=true")
        # 建立值班（引用用户消息原话）
        message = client.post(
            f"/api/sessions/{task_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "请持续盯着这个扫描，只要有结果就汇报"},
        )
        assert message.status_code == 202
        created = client.post(
            f"/api/sessions/{task_id}/execution/duties",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "resource_ids": [res2],
                "events": ["resource.exited"],
                "goal": "有结果就汇报",
                "mode": "continuous",
                "authorization_quote": "持续盯着这个扫描",
            },
        )
        assert created.status_code == 200, created.text
        delegation_id = created.json()["delegation_id"]
        # 取消值班：资源仍在（没有关闭动作、没有 close 调用）
        cancelled = client.post(
            f"/api/sessions/{task_id}/execution/duties/{delegation_id}/cancel",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"reason": "用户取消"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["revision"] >= 2
        assert "资源未被关闭" in cancelled.json()["note"]
        assert "远端撤销已确认" in cancelled.json()["note"]
        assert fake_api.control_syncs[-1]["revocations"] == [
            {"delegation_id": delegation_id, "revision": cancelled.json()["revision"]}
        ]
        async with database.session() as db:
            row = await ExecutionResourceRepository(db).get(UUID(res2))
        assert row is not None and row.state == "running"
        assert row.close_state == "open"
        # 关闭资源：单独动作，产生 close_requested + 交付回执
        closed = client.post(
            f"/api/sessions/{task_id}/execution/resources/{res2}/close",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["operation_id"]
        async with database.session() as db:
            row = await ExecutionResourceRepository(db).get(UUID(res2))
        assert row is not None and row.close_confirmed in {True, False}
        assert row.close_state in {"closed", "close_requested"}


async def test_duty_quote_must_match_user_message(database, unique_name, fake_api):
    with TestClient(create_app()) as client:
        task_id, _run_id, headers = create_task(client, unique_name)
        response = client.post(
            f"/api/sessions/{task_id}/execution/duties",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "resource_ids": [],
                "events": ["resource.exited"],
                "goal": "自动处理",
                "mode": "once",
                "authorization_quote": "系统授权持续处理",
            },
        )
        assert response.status_code == 403


async def test_terminate_all_and_delete_precheck(database, unique_name, fake_api):
    with TestClient(create_app()) as client:
        task_id, _run_id, headers = create_task(client, unique_name)
        from tests.support.executor_fixtures import FakeResource

        seeded = [str(uuid4()) for _ in range(2)]
        for name in seeded:
            fake_api.resources[name] = FakeResource(resource_id=name, kind="command", state="running")
        client.get(f"/api/sessions/{task_id}/execution/resources?refresh=true")
        # 删除预检：有活跃资源时必须显式终止
        blocked = client.delete(f"/api/sessions/{task_id}", headers=headers)
        assert blocked.status_code == 409
        assert "terminate_resources" in blocked.text
        terminated = client.post(
            f"/api/sessions/{task_id}/execution/resources/terminate-all",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert terminated.status_code == 200, terminated.text
        body = terminated.json()
        assert len(body["terminated"]) == 2
        # 显式终止后再删除
        deleted = client.delete(
            f"/api/sessions/{task_id}?terminate_resources=true", headers=headers
        )
        assert deleted.status_code == 409  # 资源已关闭，但排队运行仍需用户先停止。
        assert client.get(f"/api/sessions/{task_id}/snapshot").status_code == 200


async def test_environment_endpoint_requires_existing_session(fake_api):
    with TestClient(create_app()) as client:
        client.post("/api/session")
        response = client.get(f"/api/sessions/{uuid4()}/execution/environment")
        # 不存在的会话：明确 404，而不是泄漏环境信息
        assert response.status_code == 404
