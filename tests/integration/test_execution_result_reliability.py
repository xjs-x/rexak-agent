"""本地替身验证 Job 查询、原始日志来源及 HTTP 执行未知。"""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import JobState, ToolEffectClass
from agent_workbench.contracts.tool import JobSnapshot, ToolError
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.repositories import ExecutionResourceRepository
from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow, ToolOperationRow
from agent_workbench.tools.execution_resources import wait_for_resources
from agent_workbench.tools.jobs import export_execution_output, get_job
from agent_workbench.tools.operation import OperationRegistry
from tests.integration.test_execution_tools import fake_toolbox
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor, FakeResource

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("uncertain", [False, True])
async def test_legacy_job_query_updates_operation_and_retains_uncertainty(
    database, unique_name, uncertain
):
    with TestClient(create_app()) as client:
        session_id, run_id, _ = create_task(client, unique_name)
    job_id = uuid4()
    async with database.transaction() as db:
        op = await OperationRegistry(db).prepare(
            run_id=run_id,
            session_id=session_id,
            tool_call_id="browser",
            tool_name="browser_navigate",
            arguments={},
            effect_class=ToolEffectClass.SIDE_EFFECTING,
            policy_revision=1,
            scope_revision=1,
        )
        op.row.job_id, op.row.status = job_id, "running"
        operation_id = op.operation_id
    snapshot = JobSnapshot(
        job_id=job_id,
        operation_id=operation_id,
        kind="http_request" if uncertain else "browser",
        state=JobState.FAILED if uncertain else JobState.SUCCEEDED,
        error=ToolError(code="http_transport_error", message="incomplete chunked read")
        if uncertain
        else None,
    )
    executor = SimpleNamespace(get_job=AsyncMock(return_value=snapshot))
    box = fake_toolbox(database, session_id, cast(Any, executor), run_id=run_id)
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box))
    result = json.loads(await cast(Any, get_job).coroutine(runtime=runtime, job_id=str(job_id)))
    expected = "unknown" if uncertain else "succeeded"
    assert result["data"]["state"] == expected
    async with database.session() as db:
        operation = await db.get(ToolOperationRow, operation_id)
        job = await db.get(ToolJobRow, job_id)
        assert operation.status == job.state == expected
        assert job.sync_status == ("uncertain" if uncertain else "in_sync")
    waited = json.loads(
        await cast(Any, wait_for_resources).coroutine(
            runtime=runtime, resource_ids_json=[str(job_id)], wait_ms=0
        )
    )
    assert "get_job" in waited["error"]["message"]
    executor.get_job.assert_awaited_once()


@pytest.mark.parametrize("source", ["known", "unknown", "ambiguous"])
async def test_export_links_only_reliable_same_session_origin(database, unique_name, source):
    with TestClient(create_app()) as client:
        session_id, run_id, _ = create_task(client, unique_name)
    resource_id = uuid4()
    executor = FakeExecutor()
    executor.resources[str(resource_id)] = FakeResource(
        resource_id=str(resource_id), kind="command", output=b"local evidence\xff"
    )
    origin_id = None
    async with database.transaction() as db:
        if source != "unknown":
            for index in range(2 if source == "ambiguous" else 1):
                op = await OperationRegistry(db).prepare(
                    run_id=run_id,
                    session_id=session_id,
                    tool_call_id=f"origin-{index}",
                    tool_name="run_command",
                    arguments={},
                    effect_class=ToolEffectClass.SIDE_EFFECTING,
                    policy_revision=1,
                    scope_revision=1,
                )
                op.row.job_id = resource_id
                origin_id = op.operation_id
        await ExecutionResourceRepository(db).upsert(
            resource_id=resource_id,
            session_id=session_id,
            executor_id="fixture",
            kind="command",
            state="running",
            origin_operation_id=origin_id if source == "known" else None,
        )
    box = fake_toolbox(database, session_id, executor, run_id=run_id)
    box.artifacts = ArtifactStore(database.settings)
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box))
    result = json.loads(
        await cast(Any, export_execution_output).coroutine(runtime=runtime, job_id=str(resource_id))
    )
    async with database.session() as db:
        artifact = await db.get(ArtifactRow, UUID(result["data"]["artifact_id"]))
        assert artifact.source_operation_id == (origin_id if source == "known" else None)
        assert artifact.session_id == session_id
        assert box.artifacts.verify(artifact.relative_key, artifact.sha256)


async def test_http_response_interruption_records_unknown_without_retry():
    from workbench_executor.api import _run_http_job

    ledger = SimpleNamespace(update=AsyncMock())
    request = SimpleNamespace(payload={"url": "http://fixture.invalid/", "method": "POST"})
    request_once = AsyncMock(side_effect=httpx.RemoteProtocolError("incomplete chunked read"))
    events = []
    state = SimpleNamespace(
        ledger=ledger,
        http_sessions=SimpleNamespace(request=request_once),
        emit=lambda name, payload: events.append((name, payload)),
    )
    await _run_http_job(cast(Any, state), "job", cast(Any, request), cast(Any, None))
    request_once.assert_awaited_once()
    assert ledger.update.await_args.kwargs["state"] == "unknown"
    assert ledger.update.await_args.kwargs["error_code"] == "http_transport_error"
    assert events[0][0] == "job.unknown"
