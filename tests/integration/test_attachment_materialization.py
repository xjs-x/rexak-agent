"""隔离数据库中的上传→绑定→Main 工具→Worker 委派；无模型。"""

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.contracts.delegation import DelegationInput
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.execution.client import ExecutorConnectionError, HttpExecutorClient
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import AgentDelegationRow, RunRow
from agent_workbench.tools.attachment_delivery import deliver_attachment
from agent_workbench.tools.attachments import list_attachments, materialize_attachment
from agent_workbench.tools.runtime import ResourceLocks, Toolbox
from tests.integration.test_attachments import upload
from tests.integration.test_ultra_delegations import make_manager
from tests.support.executor_fixtures import fixture_capabilities
from tests.unit.test_attachment_delivery import delivery, flask_zip  # noqa: F401

pytestmark = pytest.mark.integration


async def test_uploaded_zip_main_and_worker_delivery(database, settings, delivery, monkeypatch):  # noqa: F811
    state, http, _, _ = delivery
    cfg = settings.model_copy(update={"ultra_worker_execution_enabled": True})
    manager = await make_manager(database, cfg)
    raw = flask_zip()
    with TestClient(create_app()) as api:
        headers = {"X-Workbench-CSRF": api.post("/api/session").json()["csrf_token"]}
        response = upload(api, manager.session_id, headers, raw, "flask.zip")
        assert response.status_code == 201, response.text
        aid = response.json()["attachment_id"]
        response = api.post(f"/api/sessions/{manager.session_id}/messages",
            json={"content": "synthetic attachment analysis", "attachment_ids": [aid]},
            headers={**headers, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 202, response.text
        assert api.get(f"/api/sessions/{manager.session_id}/attachments/{aid}/content").content == raw

    remote = object.__new__(HttpExecutorClient)
    remote._data = http
    remote._control = http
    async with database.session() as db:
        current = await db.get(RunRow, manager.run_id)
        version = current.control_version

    async def capabilities():
        caps = fixture_capabilities()
        caps.execution["input_files"] = "available"
        return caps

    monkeypatch.setattr(remote, "capabilities", capabilities)
    box = Toolbox(db=database, settings=cfg, session_id=manager.session_id, run_id=manager.run_id,
        owner="test-owner", owner_generation=1, control_version=version, executor=remote,
        scope=ScopePolicy(ScopeSnapshot(mode="instruction")), artifacts=ArtifactStore(cfg),
        spans=SpanRecorder(), locks=ResourceLocks(cfg))
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box), tool_call_id="main-input")
    catalogue = json.loads(await cast(Any, list_attachments).coroutine(runtime))
    assert catalogue["attachments"][0]["attachment_id"] == aid
    main = json.loads(await cast(Any, materialize_attachment).coroutine(runtime, aid))
    assert main["status"] == "succeeded", main
    original = box.artifacts.resolve((await _attachment(database, aid)).relative_key)
    assert original.read_bytes() == raw
    replay = json.loads(await cast(Any, materialize_attachment).coroutine(runtime, aid))
    assert replay["operation_id"] == main["operation_id"] and replay["deduplicated"]
    original_delivery = remote.execution_input_file
    submits = []

    async def lost_response(env, path):
        submits.append(env["operation_id"])
        await original_delivery(env, path)
        raise ExecutorConnectionError("injected lost response")

    monkeypatch.setattr(remote, "execution_input_file", lost_response)
    runtime.tool_call_id = "lost-input-response"
    lost = json.loads(await cast(Any, materialize_attachment).coroutine(runtime, aid))
    assert lost["status"] == "unknown" and submits[0] in lost["note"]
    recovered = json.loads(await cast(Any, materialize_attachment).coroutine(runtime, aid))
    assert recovered["status"] == "succeeded" and len(submits) == 1
    monkeypatch.setattr(remote, "execution_input_file", original_delivery)

    sent = []

    async def submit(_self, ident, payload):
        # This is the boundary immediately before an A2A Worker is started.
        info = payload["input_files"][0]
        actual = state.settings.workspace_root / str(manager.session_id) / ".rexak/workers" / str(ident) / info["relative_path"]
        assert actual.read_bytes() == raw
        assert info["worker_workspace_id"] == str(ident)
        sent.append(payload)
        return ident

    async def enter(self):
        return self

    async def project(ident, _task):
        return await manager.read(ident)

    monkeypatch.setattr(A2AWorkerClient, "__aenter__", enter)
    monkeypatch.setattr(A2AWorkerClient, "submit", submit)
    monkeypatch.setattr(manager, "_project", project)

    async def deliver(ident, ids):
        return [(await deliver_attachment(box, UUID(item), f"worker:{ident}:{item}", worker_id=ident))["input_file"] for item in ids]

    for call in ("worker-one", "worker-two"):
        result = await manager.delegate(call, DelegationInput(objective="read synthetic source", attachment_ids=[UUID(aid)]),
            owner="test-owner", generation=1, control_version=version, input_delivery=deliver)
        assert result.state == "submitting"
    assert sent[0]["input_files"][0]["worker_workspace_id"] != sent[1]["input_files"][0]["worker_workspace_id"]
    assert all(item["attachment_ids"] == [aid] for item in sent)

    async def fail(*_args):
        raise RuntimeError("injected delivery failure")

    result = await manager.delegate("failed-worker", DelegationInput(objective="no start", attachment_ids=[UUID(aid)]),
        owner="test-owner", generation=1, control_version=version, input_delivery=fail)
    assert result.state == "failed" and len(sent) == 2
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, result.id)
        assert row.a2a_task_id is None and "attachment_delivery_failed" in row.diagnostic
    with pytest.raises(ApiError):
        await manager.delegate("foreign", DelegationInput(objective="no access", attachment_ids=[uuid4()]),
            owner="test-owner", generation=1, control_version=version, input_delivery=deliver)
    denied = json.loads(await cast(Any, materialize_attachment).coroutine(runtime, str(uuid4())))
    assert denied["status"] == "error"


async def _attachment(database, identifier):
    from agent_workbench.persistence.tables import AttachmentRow

    async with database.session() as db:
        return await db.get(AttachmentRow, UUID(identifier))
