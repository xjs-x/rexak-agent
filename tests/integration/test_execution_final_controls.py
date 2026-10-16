"""最终控制边界：终止保留资源、拒绝隐藏未确认资源、原始日志证据。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime

from agent_workbench.api.app import create_app
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.repositories import ArtifactRepository, ExecutionResourceRepository
from agent_workbench.persistence.tables import RunRow
from agent_workbench.runtime.execution_control import terminate_session_resources
from agent_workbench.tools.jobs import export_execution_output
from tests.integration.test_execution_api import FakeExecutorSession
from tests.integration.test_execution_tools import fake_toolbox
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor, FakeResource

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_api(monkeypatch):
    import agent_workbench.api.execution as api
    executor = FakeExecutor()
    monkeypatch.setattr(api, "_ExecutorSession", FakeExecutorSession(executor))
    return executor


async def test_termination_closes_prior_run_resource_with_stable_identity(database, unique_name):
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        resource_id = uuid4()
        executor = FakeExecutor()
        executor.resources[str(resource_id)] = FakeResource(resource_id=str(resource_id), kind="terminal")
        async with database.transaction() as db:
            run = await db.get(RunRow, run_id)
            assert run is not None
            await ExecutionResourceRepository(db).upsert(
                resource_id=resource_id, session_id=task_id, executor_id="kali-01",
                kind="terminal", state="running", origin_run_id=run_id)
        assert await terminate_session_resources(database, executor, run)
        assert executor.envelopes[-1]["action"] == "terminal_close"
        assert executor.resources[str(resource_id)].terminated
        count = len(executor.envelopes)
        assert await terminate_session_resources(database, executor, run)
        assert len(executor.envelopes) == count


async def test_delete_keeps_unknown_resources_visible(database, unique_name, fake_api, monkeypatch):
    with TestClient(create_app()) as client:
        task_id, _, headers = create_task(client, unique_name)
        resource_id = str(uuid4())
        fake_api.resources[resource_id] = FakeResource(resource_id=resource_id, kind="command")
        client.get(f"/api/sessions/{task_id}/execution/resources?refresh=true")

        async def unconfirmed(_self, envelope):
            return {"operation_id": envelope["operation_id"], "resource_id": resource_id,
                    "status": "unknown", "process_state": "closed", "process_confirmed_exited": False}

        monkeypatch.setattr(FakeExecutor, "execution_submit", unconfirmed)
        response = client.delete(f"/api/sessions/{task_id}?terminate_resources=true", headers=headers)
        assert response.status_code == 409
        assert "未确认" in response.text
        assert client.get(f"/api/sessions/{task_id}/snapshot").status_code == 200


async def test_raw_output_export_creates_immutable_artifact(database, unique_name):
    import json

    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        resource_id = uuid4()
        executor = FakeExecutor()
        raw = b"\xff\x00" + "原始中文".encode()
        executor.resources[str(resource_id)] = FakeResource(resource_id=str(resource_id), kind="command", output=raw)
        async with database.transaction() as db:
            await ExecutionResourceRepository(db).upsert(
                resource_id=resource_id, session_id=task_id, executor_id="kali-01", kind="command", state="running")
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        toolbox.artifacts = ArtifactStore(database.settings)
        runtime = cast(ToolRuntime[Any], SimpleNamespace(context=SimpleNamespace(toolbox=toolbox)))
        result = json.loads(await cast(Any, export_execution_output).coroutine(runtime=runtime, job_id=str(resource_id)))
        data = result["data"]
        assert data["sha256"] == hashlib.sha256(raw).hexdigest()
        assert "data_base64" not in data
        async with database.session() as db:
            artifact = await ArtifactRepository(db).get(UUID(data["artifact_id"]))
        assert artifact is not None and artifact.byte_size == len(raw)
        assert toolbox.artifacts.verify(artifact.relative_key, artifact.sha256)


async def test_template_versions_are_immutable_and_parameters_are_data(database, unique_name):
    import asyncio
    import json

    from agent_workbench.tools.execution_resources import (
        register_execution_template,
        run_execution_template,
    )
    with TestClient(create_app()) as client:
        task_id, run_id, _ = create_task(client, unique_name)
        executor = FakeExecutor()
        toolbox = fake_toolbox(database, task_id, executor, run_id=run_id)
        runtime = SimpleNamespace(context=SimpleNamespace(toolbox=toolbox), tool_call_id="template-register")
        first = json.loads(await cast(Any, register_execution_template).coroutine(
            runtime=runtime, name="fixture", kind="command", description="fixture",
            argv_json=["printf", "%s", "{value}"], param_schema_json={"required": ["value"]}))
        second = json.loads(await cast(Any, register_execution_template).coroutine(
            runtime=runtime, name="fixture", kind="command", description="fixture v2",
            argv_json=["printf", "%s!", "{value}"], param_schema_json={"required": ["value"]}))
        assert first["data"]["revision"] == 1 and second["data"]["revision"] == 2
        runtime.tool_call_id = "template-run-one"
        value = "中文 ; $(fixture) ' space"
        calls = await asyncio.gather(*(cast(Any, run_execution_template).coroutine(
            runtime=runtime, name="fixture", revision=1, params_json={"value": value}, background=True)
            for _ in range(2)))
        result, concurrent = [json.loads(item) for item in calls]
        assert concurrent["operation_id"] == result["operation_id"]
        assert result["status"] == "accepted", result
        arguments = executor.envelopes[-1]["arguments"]
        assert arguments["argv"] == ["printf", "%s", value]
        assert arguments["template_revision"] == 1
        assert arguments["template_hash"] == first["data"]["content_hash"]
        repeat = json.loads(await cast(Any, run_execution_template).coroutine(
            runtime=runtime, name="fixture", revision=1, params_json={"value": value}, background=True))
        assert repeat["operation_id"] == result["operation_id"]
        assert len(executor.resources) == 1
