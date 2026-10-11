"""工具进度的历史、执行端状态、详情及字段脱敏。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.observability.tool_activity import emit_tool_activity
from tests.integration.test_instruction_scope import create_task

pytestmark = pytest.mark.integration


async def test_activity_survives_refresh_and_details_are_redacted(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        toolbox = SimpleNamespace(db=database, session_id=task_id, run_id=run_id)
        call = {
            "id": "call-visible",
            "name": "http_request",
            "args": {
                "url": "https://fixture.example.test/",
                "method": "HEAD",
                "headers_json": '{"Authorization":"Bearer invisible-value"}',
                "password": "invisible-password",
            },
        }
        await emit_tool_activity(toolbox, call, phase="started", status="preparing")
        first = client.get(f"/api/sessions/{task_id}/tool-activity?limit=1")
        assert first.status_code == 200, first.text
        item = first.json()["activities"][0]
        assert item["status"] == "preparing" and item["finished_at"] is None
        await emit_tool_activity(
            toolbox, call, phase="finished", status="failed", output="证书验证失败"
        )
        item = client.get(f"/api/sessions/{task_id}/tool-activity").json()["activities"][0]
        assert item["status"] == "failed" and item["finished_at"] is not None
        detail = client.get(f"/api/sessions/{task_id}/tool-activity/{run_id}/call-visible")
        assert detail.status_code == 200, detail.text
        assert "HEAD" in detail.text and "证书验证失败" in detail.text
        assert "invisible-value" not in detail.text and "invisible-password" not in detail.text
        assert "redacted" in detail.text
        other_task, _, _ = create_task(client, unique_name + "-other")
        assert (
            client.get(
                f"/api/sessions/{other_task}/tool-activity/{run_id}/call-visible"
            ).status_code
            == 404
        )


async def test_activity_replay_does_not_use_previous_completion(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        toolbox = SimpleNamespace(db=database, session_id=task_id, run_id=run_id)
        call = {"id": "replay", "name": "read_file", "args": {"path": "example.txt"}}
        await emit_tool_activity(toolbox, call, phase="started", status="preparing")
        await emit_tool_activity(
            toolbox, call, phase="finished", status="completed", output="上一轮结果"
        )
        await emit_tool_activity(toolbox, call, phase="started", status="preparing")
        items = client.get(f"/api/sessions/{task_id}/tool-activity").json()["activities"]
        assert len(items) == 1 and items[0]["status"] == "preparing"
        detail = client.get(f"/api/sessions/{task_id}/tool-activity/{run_id}/replay").json()
        assert detail["result_text"] == "尚未返回结果"
        for _ in range(3):
            await emit_tool_activity(
                toolbox, {**call, "id": str(uuid4())}, phase="started", status="preparing"
            )
        page = client.get(f"/api/sessions/{task_id}/tool-activity?limit=2").json()
        assert len(page["activities"]) == 2 and page["next_offset"] == 2
        rest = client.get(f"/api/sessions/{task_id}/tool-activity?limit=2&offset=2").json()
        assert len(rest["activities"]) == 2 and rest["next_offset"] is None


async def test_failed_http_uses_executor_error_without_downloading_response(database, unique_name):
    from datetime import UTC, datetime
    from uuid import UUID

    from agent_workbench.contracts.enums import JobState, OperationStatus
    from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
    from agent_workbench.contracts.tool import JobReceipt, JobSnapshot, ToolError
    from agent_workbench.domain.scope import ScopePolicy
    from agent_workbench.persistence.repositories import compute_request_digest
    from agent_workbench.persistence.tables import ToolOperationRow
    from agent_workbench.tools.http import HttpRequestArgs, run_http_request
    from tests.integration.test_tool_dispatch import _build_context

    class Executor:
        submitted = None

        async def submit(self, operation):
            self.submitted = operation
            return JobReceipt(
                job_id=uuid4(),
                operation_id=UUID(operation["operation_id"]),
                state=JobState.RECEIVED,
                accepted_at=datetime.now(UTC),
            )

        async def get_job(self, job_id):
            assert self.submitted is not None
            return JobSnapshot(
                job_id=job_id,
                operation_id=UUID(self.submitted["operation_id"]),
                kind="http_request",
                state=JobState.FAILED,
                error=ToolError(
                    code="tls_certificate_error", message="self-signed certificate", retryable=False
                ),
            )

        async def download_artifact(self, *_args, **_kwargs):
            raise AssertionError("没有响应的失败请求不应下载 response.meta.json")

    executor = Executor()
    context = await _build_context(database, unique_name, executor_stub=executor)
    context.toolbox.scope = ScopePolicy(
        ScopeSnapshot(rules=[ScopeRule(scheme="https", host="127.0.0.1", port=9443)])
    )
    result = await run_http_request(
        HttpRequestArgs(url="https://127.0.0.1:9443/"), context.toolbox, "failed-tls"
    )
    assert result.status is OperationStatus.FAILED
    assert result.error and result.error.code == "tls_certificate_error"
    assert "self-signed certificate" in result.summary
    assert result.job_id and result.operation_id.int != 0 and result.artifact_refs == []
    assert executor.submitted is not None
    assert executor.submitted["payload"]["verify_tls"] is True
    async with database.session() as db:
        operation = await db.get(ToolOperationRow, result.operation_id)
        assert operation.status == "failed", "原来的工件下载异常会让控制端操作一直留在 running"
        previous_arguments = HttpRequestArgs(url="https://127.0.0.1:9443/").model_dump(
            exclude={"note", "verify_tls"}
        )
        assert operation.request_digest == compute_request_digest(
            {"tool": "http_request", "arguments": previous_arguments}
        )


async def test_legacy_operation_details_read_official_history(database, settings, unique_name):
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.persistence.tables import SessionRow
    from agent_workbench.tools.operation import OperationRegistry
    from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

    @tool
    def legacy_fixture(url: str) -> str:
        """不联网的历史调用 fixture。"""
        return "已读取 fixture " + url

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        async with database.transaction() as db:
            task = await db.get(SessionRow, task_id)
            await OperationRegistry(db).prepare(
                run_id=run_id,
                session_id=task_id,
                tool_call_id="legacy-one",
                tool_name="legacy_fixture",
                arguments={"url": "https://history.example.test/"},
                effect_class=ToolEffectClass.READ_ONLY,
                policy_revision=1,
                scope_revision=1,
            )
        async with open_checkpointer_connection(settings) as connection:
            graph = create_agent(
                ScriptedChatModel(
                    responses=[
                        tool_call(
                            "legacy_fixture", {"url": "https://history.example.test/"}, "legacy-one"
                        ),
                        final_text("结束"),
                    ]
                ),
                tools=[legacy_fixture],
                checkpointer=AsyncPostgresSaver(connection),
            )
            await graph.ainvoke(
                {"messages": [{"role": "user", "content": "fixture"}]},
                {"configurable": {"thread_id": task.thread_id}},
            )
        result = client.get(f"/api/sessions/{task_id}/tool-activity/{run_id}/legacy-one")
        assert result.status_code == 200, result.text
        assert "https://history.example.test/" in result.json()["request_text"]
        assert "已读取 fixture" in result.json()["result_text"]


async def test_executor_terminal_status_overrides_stuck_operation_and_stale_sync(
    database, unique_name
):
    from datetime import UTC, datetime

    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.persistence.repositories import ToolJobRepository
    from agent_workbench.persistence.tables import ToolJobRow
    from agent_workbench.tools.operation import OperationRegistry

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        job_id = uuid4()
        finished = datetime.now(UTC)
        async with database.transaction() as db:
            op = await OperationRegistry(db).prepare(
                run_id=run_id,
                session_id=task_id,
                tool_call_id="job-failed",
                tool_name="http_request",
                arguments={"url": "https://fixture.example.test/"},
                effect_class=ToolEffectClass.READ_ONLY,
                policy_revision=1,
                scope_revision=1,
            )
            op.row.status = "running"
            op.row.job_id = job_id
            await ToolJobRepository(db).upsert(
                job_id=job_id,
                operation_id=op.operation_id,
                executor_id="fixture",
                state="failed",
                sync_status="in_sync",
                executor_finished_at=finished,
            )
        page = client.get(f"/api/sessions/{task_id}/tool-activity").json()
        assert page["activities"][0]["status"] == "failed"
        assert page["activities"][0]["finished_at"] is not None
        async with database.transaction() as db:
            job = await db.get(ToolJobRow, job_id)
            job.sync_status = "stale"
        page = client.get(f"/api/sessions/{task_id}/tool-activity").json()
        assert page["activities"][0]["status"] == "unknown", "失联后的旧状态不能显示成已确认终态"
