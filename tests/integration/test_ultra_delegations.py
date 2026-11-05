"""Real PostgreSQL + Python/Node official A2A SDKs + Pi; no model provider or target traffic."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from a2a.types import TaskState
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.delegation import DelegationInput
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.agent_delegations import DelegationManager, reconcile_inactive_parents
from agent_workbench.domain.run_state import create_run
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.repositories import SessionRepository
from agent_workbench.persistence.tables import AgentDelegationRow, RunRow

pytestmark = pytest.mark.integration
TOKEN = "isolated-mock-worker-token-32-characters"


async def test_sdk_mock_round_trip_before_pi(settings, tmp_path):
    """Separate protocol-only control; does not import or run Pi in the Node process."""
    from google.protobuf.json_format import MessageToDict

    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    config = settings.model_copy(update={"ultra_worker_url": f"http://127.0.0.1:{port}", "ultra_worker_token": TOKEN})
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
    env.update(MOCK_A2A_PORT=str(port), MOCK_A2A_TOKEN=TOKEN)
    node = shutil.which("node")
    assert node
    with (tmp_path / "pure-sdk.log").open("wb") as output:
        process = subprocess.Popen([node, "--import", "tsx", "test/mock-a2a-server.ts"],
            cwd=REPO_ROOT / "worker", env=env, stdout=output, stderr=output)
        try:
            for _ in range(100):
                assert process.poll() is None
                try:
                    async with A2AWorkerClient(config) as client:
                        await client.connect()
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
            request_id = uuid4()
            async with A2AWorkerClient(config) as client:
                task = await client.submit(request_id, {"objective": "protocol fixture", "context_refs": []})
                assert task.id != str(request_id) and task.status.state == TaskState.TASK_STATE_COMPLETED
                queried = await client.get(task.id)
                assert MessageToDict(queried.artifacts[0].parts[0].data)["summary"] == "Pure official SDK mock round trip"
        finally:
            process.terminate()
            process.wait(timeout=10)


class LocalWorker:
    def __init__(self, settings, path: Path):
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            self.port = bound.getsockname()[1]
        self.settings = settings.model_copy(update={
            "ultra_worker_url": f"http://127.0.0.1:{self.port}", "ultra_worker_token": TOKEN,
        })
        self.log = path.open("wb")
        self.process = None

    def start(self, *, delay=20, fail=False):
        entry = (REPO_ROOT / "worker/dist/server.js").as_uri()
        code = f"""import {{createWorkerServer}} from {json.dumps(entry)};
const runtime=await createWorkerServer({{databaseUrl:process.env.REXAK_WORKER_DATABASE_URL,token:process.env.REXAK_WORKER_TOKEN,baseUrl:'http://127.0.0.1:{self.port}',delayMs:{delay},fail:{str(fail).lower()}}});
runtime.app.listen({self.port},'127.0.0.1');"""
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
        env.update(REXAK_WORKER_TOKEN=TOKEN, REXAK_WORKER_DATABASE_URL=make_url(
            self.settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False))
        node = shutil.which("node")
        assert node and (REPO_ROOT / "worker/dist/server.js").exists(), "Build Worker before cross-language tests"
        self.process = subprocess.Popen([node, "--input-type=module", "-e", code], env=env,
                                        stdout=self.log, stderr=self.log)
        for _ in range(100):
            assert self.process.poll() is None, "Worker failed; inspect isolated worker.log"
            try:
                with httpx.Client(trust_env=False, timeout=0.2) as client:
                    response = client.get(f"http://127.0.0.1:{self.port}/health", headers={"Authorization": f"Bearer {TOKEN}"})
                    if response.status_code == 200:
                        return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("Worker startup timed out")

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.process = None


@pytest.fixture
def local_worker(settings, database, tmp_path):
    worker = LocalWorker(settings, tmp_path / "worker.log")
    worker.start()
    try:
        yield worker
    finally:
        worker.stop()
        worker.log.close()


async def make_manager(database, settings):
    async with database.transaction() as db:
        parent = await SessionRepository(db).create(project_id=None, title="Ultra fixture",
            scope_snapshot={}, policy_snapshot={}, execution_mode="ultra")
        run = await create_run(db, session_id=parent.id, objective="synthetic goal", provider_id="mock",
                              model_name="mock", config_snapshot={})
        run.status, run.owner, run.owner_generation = "running", "test-owner", 1
        run.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        sid, rid = parent.id, run.id
    return DelegationManager(database, settings, sid, rid)


async def delegate(manager, call="call-1", objective="synthetic alpha"):
    return await manager.delegate(call, DelegationInput(objective=objective),
        owner="test-owner", generation=1, control_version=0)


async def test_real_a2a_pi_artifact_and_durable_duplicate(database, local_worker):
    manager = await make_manager(database, local_worker.settings)
    first = await delegate(manager)
    assert first.state in {"accepted", "working", "completed"}
    result = await manager.wait(first.id, 3)
    assert result.state == "completed" and result.result_artifact_id, result.model_dump()
    assert "synthetic alpha" not in result.summary
    assert "2 synthetic observations" in result.summary
    duplicate = await delegate(manager)
    assert duplicate.id == first.id and duplicate.a2a_task_id == result.a2a_task_id
    assert str(first.id) != result.a2a_task_id
    async with database.session() as db:
        count = await db.scalar(text("SELECT count(*) FROM ultra_worker.requests WHERE request_id=:id"), {"id": first.id})
        assert count == 1
    with pytest.raises(ApiError):
        await delegate(manager, objective="conflicting retry")


async def test_response_lost_reconciles_without_resubmit(database, local_worker, monkeypatch):
    manager = await make_manager(database, local_worker.settings)
    original = A2AWorkerClient.submit

    async def delivered_then_lost(self, request_id, payload):
        await original(self, request_id, payload)
        raise httpx.ReadTimeout("injected lost response")

    monkeypatch.setattr(A2AWorkerClient, "submit", delivered_then_lost)
    record = await delegate(manager)
    assert record.state == "unknown" and record.a2a_task_id is None
    monkeypatch.setattr(A2AWorkerClient, "submit", original)
    result = await manager.wait(record.id, 3)
    assert result.state == "completed" and result.worker_id


async def test_wire_duplicate_and_cancel_after_completion(database, local_worker):
    request_id = uuid4()
    payload = {"objective": "duplicate over real A2A", "context_refs": []}
    async with A2AWorkerClient(local_worker.settings) as first, A2AWorkerClient(local_worker.settings) as second:
        left, right = await asyncio.gather(first.submit(request_id, payload), second.submit(request_id, payload))
        assert left.id == right.id
        async with database.session() as db:
            assert await db.scalar(text("SELECT count(*) FROM ultra_worker.requests WHERE request_id=:id"), {"id": request_id}) == 1
        for _ in range(40):
            task = await first.get(left.id)
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.05)
        result = await second.cancel(request_id, left.id)
        assert result and result.status.state == TaskState.TASK_STATE_COMPLETED


async def test_control_outbox_survives_quick_pause_resume(database, local_worker):
    from agent_workbench.contracts.enums import CommandKind
    from agent_workbench.runtime.commands import submit_control

    local_worker.stop()
    local_worker.start(delay=2000)
    manager = await make_manager(database, local_worker.settings)
    record = await delegate(manager)
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        assert run is not None
        await submit_control(db, run=run, kind=CommandKind.PAUSE, idempotency_key="pause-once")
        run.status = "running"  # Resumed before background reconciler sees the pause.
    assert (await manager.read(record.id)).cancel_requested
    await reconcile_inactive_parents(database, local_worker.settings)
    assert (await manager.refresh(record.id)).state == "cancelled"


async def test_cancel_wait_timeout_and_parent_pause(database, local_worker):
    local_worker.stop()
    local_worker.start(delay=1000)
    manager = await make_manager(database, local_worker.settings)
    record = await delegate(manager)
    waiting = await manager.wait(record.id, 0)
    assert waiting.state in {"working", "accepted"}
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        run.status = "paused"
    await reconcile_inactive_parents(database, local_worker.settings)
    result = await manager.refresh(record.id)
    assert result.state == "cancelled"
    async with A2AWorkerClient(local_worker.settings) as client:
        assert result.a2a_task_id
        task = await client.get(result.a2a_task_id)
        assert task.status.state == TaskState.TASK_STATE_CANCELED


async def test_server_restart_unknown_and_no_pi_reexecution(database, local_worker):
    local_worker.stop()
    local_worker.start(delay=5000)
    manager = await make_manager(database, local_worker.settings)
    record = await delegate(manager)
    local_worker.stop()
    local_worker.start()
    result = await manager.refresh(record.id)
    assert result.state == "unknown"
    assert result.remote_state == "TASK_STATE_UNSPECIFIED"
    assert result.diagnostic == "worker_restart_no_pi_resume"
    repeated = await delegate(manager)
    assert repeated.a2a_task_id == result.a2a_task_id and repeated.state == "unknown"
    # Restart/lock loss is not process-exit proof for an old claimed loop.
    assert (await manager.cancel(record.id)).state == "cancel_requested"


async def test_worker_provider_failure(database, local_worker):
    local_worker.stop()
    local_worker.start(fail=True)
    manager = await make_manager(database, local_worker.settings)
    record = await delegate(manager)
    result = await manager.wait(record.id, 3)
    assert result.state == "failed"
    assert result.result_artifact_id and "failure" in result.summary.lower()


async def test_cross_session_and_stale_owner_denied(database, local_worker):
    manager = await make_manager(database, local_worker.settings)
    other = await make_manager(database, local_worker.settings)
    record = await delegate(manager)
    with pytest.raises(ApiError):
        await other.read(record.id)
    with pytest.raises(ApiError):
        await other.cancel(record.id)
    with pytest.raises(ApiError):
        await manager.delegate("stale", DelegationInput(objective="x"), owner="stale", generation=0, control_version=0)
    with pytest.raises(ApiError):
        await manager.delegate("stale-version", DelegationInput(objective="x"), owner="test-owner", generation=1, control_version=5)
    with pytest.raises(ApiError):
        await manager.delegate("foreign-ref", DelegationInput(objective="x", context_refs=[uuid4()]), owner="test-owner", generation=1, control_version=0)
    await manager.cancel(record.id)


async def test_unavailable_unknown_and_cancellation_tombstone(database, local_worker):
    manager = await make_manager(database, local_worker.settings)
    local_worker.stop()
    record = await delegate(manager)
    assert record.state == "unknown"
    cancelled = await manager.cancel(record.id)
    assert cancelled.state == "cancel_requested"
    local_worker.start()
    cancelled = await manager.cancel(record.id)
    assert cancelled.state == "cancelled"
    async with A2AWorkerClient(local_worker.settings) as client:
        with pytest.raises(Exception, match="409"):
            await client.submit(record.id, {"objective": record.objective, "context_refs": []})


async def test_untrusted_artifact_never_becomes_evidence(database, local_worker, monkeypatch):
    manager = await make_manager(database, local_worker.settings)
    record = await delegate(manager)
    assert record.a2a_task_id
    task = None
    async with A2AWorkerClient(local_worker.settings) as client:
        for _ in range(30):
            task = await client.get(record.a2a_task_id)
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.05)
    assert task is not None
    task.artifacts[0].parts[0].data.struct_value.fields["task_id"].string_value = "forged-task"
    projected = await manager._project(record.id, task)
    assert projected.state == "completed" and projected.result_artifact_id is None
    assert projected.diagnostic is not None and projected.diagnostic.startswith("invalid_worker_artifact:")
    assert await manager.cancel_all()
    assert projected.remote_state == "TASK_STATE_COMPLETED"


async def test_api_modes_persist_and_standard_ignores_worker(database):
    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        base = {"title": "mode fixture", "initial_objective": "synthetic", "draft": True}
        old = client.post("/api/sessions", headers=headers, json=base)
        assert old.status_code == 201 and old.json()["session"]["execution_mode"] == "standard"
        ultra = client.post("/api/sessions", headers=headers, json={**base, "execution_mode": "ultra"})
        assert ultra.status_code == 201
        sid = ultra.json()["session"]["id"]
        assert client.get(f"/api/sessions/{sid}/snapshot").json()["session"]["execution_mode"] == "ultra"
        assert client.get(f"/api/sessions/{sid}/delegations").json()["tasks"] == []
        assert client.post("/api/sessions", headers=headers, json={**base, "execution_mode": "invalid"}).status_code == 422
    async with database.transaction() as db:
        run = await create_run(db, session_id=UUID(sid), objective="x", provider_id="mock", model_name="mock", config_snapshot={})
        assert run.config_snapshot["execution_mode"] == "ultra"
    async with database.transaction() as db:
        with pytest.raises(ApiError):
            await create_run(db, session_id=UUID(old.json()["session"]["id"]), objective="x", provider_id="mock", model_name="mock", config_snapshot={"execution_mode": "ultra"})


async def test_main_create_agent_delegates_and_collects(database, local_worker):
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from agent_workbench.agent.context import RunContext
    from agent_workbench.tools.delegation import DELEGATION_TOOLS
    from agent_workbench.tools.runtime import Toolbox

    class MainMock(BaseChatModel):
        @property
        def _llm_type(self):
            return "ultra-main-mock"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            outputs = [m for m in messages if isinstance(m, ToolMessage)]
            if not outputs:
                message = AIMessage(content="", tool_calls=[{"id": "main-delegate", "name": "delegate_task", "args": {"objective": "synthetic end-to-end"}}])
            elif len(outputs) == 1:
                message = AIMessage(content="", tool_calls=[{"id": "main-wait", "name": "wait_delegated_task", "args": {"delegation_id": json.loads(str(outputs[0].content))["id"], "seconds": 3}}])
            else:
                result = json.loads(str(outputs[-1].content))
                assert result["state"] == "completed" and result["result_artifact_id"]
                message = AIMessage(content="Collected isolated Mock Pi result")
            return ChatResult(generations=[ChatGeneration(message=message)])

    manager = await make_manager(database, local_worker.settings)

    async def check(*_args):
        return None

    from typing import Any, cast

    from agent_workbench.contracts.identity import ScopeSnapshot
    from agent_workbench.domain.scope import ScopePolicy
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.observability.spans import SpanRecorder
    from agent_workbench.tools.runtime import ResourceLocks

    box = Toolbox(db=database, settings=local_worker.settings, session_id=manager.session_id,
        run_id=manager.run_id, owner="test-owner", owner_generation=1, control_version=0,
        scope=ScopePolicy(ScopeSnapshot(mode="instruction")), executor=cast(Any, None),
        artifacts=ArtifactStore(local_worker.settings), spans=SpanRecorder(),
        locks=ResourceLocks(local_worker.settings), control_check=check)
    graph = create_agent(MainMock(), tools=DELEGATION_TOOLS, context_schema=RunContext)
    result = await graph.ainvoke({"messages": [{"role": "user", "content": "delegate synthetic work"}]}, context=RunContext(toolbox=box))
    assert result["messages"][-1].content == "Collected isolated Mock Pi result"
    async with database.session() as db:
        assert await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.parent_run_id == manager.run_id))
