"""Goal handoff and native Pi provider over real A2A; isolated database, synthetic transport."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
from uuid import UUID, uuid4

import httpx
import pytest
from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.delegation import DelegationInput, WorkerResult
from agent_workbench.contracts.errors import ApiError
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import AgentDelegationRow, ArtifactRow
from tests.integration.test_ultra_delegations import TOKEN, make_manager

pytestmark = pytest.mark.integration


@pytest.fixture
def compatible_worker(settings, tmp_path):
    processes = []

    def start(*, hang=False, timeout=5000):
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        cfg = settings.model_copy(update={"ultra_worker_url": f"http://127.0.0.1:{port}", "ultra_worker_token": TOKEN})
        env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
        env.update(REXAK_WORKER_DATABASE_URL=make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False),
                   REXAK_WORKER_TOKEN=TOKEN, FIXTURE_PORT=str(port), FIXTURE_PROVIDER_HANG=str(hang).lower(), FIXTURE_MODEL_TIMEOUT=str(timeout))
        node = shutil.which("node")
        assert node
        handle = (tmp_path / f"native-{port}.log").open("wb")
        process = subprocess.Popen([node, "--import", "tsx", "test/compatible-a2a-server.ts"], cwd=REPO_ROOT / "worker",
                                   env=env, stdout=handle, stderr=handle)
        processes.append((process, handle))
        for _ in range(150):
            assert process.poll() is None
            try:
                with httpx.Client(trust_env=False, timeout=0.2) as client:
                    result = client.get(f"{cfg.ultra_worker_url}/health", headers={"Authorization": f"Bearer {TOKEN}"})
                if result.status_code == 200:
                    return cfg
            except httpx.HTTPError:
                pass
            import time

            time.sleep(0.05)
        raise AssertionError("Native Pi fixture startup timed out")

    yield start
    for process, handle in processes:
        process.terminate()
        process.wait(timeout=10)
        handle.close()


async def test_native_provider_handoff_a2a_usage_and_replay(database, compatible_worker, monkeypatch):
    cfg = compatible_worker()
    manager = await make_manager(database, cfg)
    stored = ArtifactStore(cfg).write_bytes(b"synthetic evidence", media_type="text/plain")
    ref = uuid4()
    async with database.transaction() as db:
        await ArtifactRepository(db).create(artifact_id=ref, sha256=stored.sha256, byte_size=stored.byte_size,
            media_type=stored.media_type, relative_key=stored.relative_key, session_id=manager.session_id,
            source_operation_id=None, captured_at=stored.captured_at, sensitivity="internal", availability="available")
    payloads = []
    original = A2AWorkerClient.submit

    async def capture(self, request_id, payload):
        payloads.append(payload)
        return await original(self, request_id, payload)

    monkeypatch.setattr(A2AWorkerClient, "submit", capture)
    value = DelegationInput(objective="Explore synthetic observations", background="Necessary background marker",
                            completion_requirements=["Record two synthetic observations"], context_refs=[ref])
    first = await manager.delegate("native-1", value, owner="test-owner", generation=1, control_version=0)
    result = await manager.wait(first.id, 5)
    assert result.state == "completed" and result.result_artifact_id and result.context_id
    assert payloads[0]["background"] == value.background
    assert payloads[0]["completion_requirements"] == value.completion_requirements
    assert payloads[0]["context_sources"][0] == {"artifact_id": str(ref), "sha256": stored.sha256,
        "byte_size": 18, "media_type": "text/plain", "captured_at": stored.captured_at.isoformat(), "source_operation_id": None}
    assert payloads[0]["context_id"] == str(result.context_id)
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, first.id)
        assert row and row.handoff_snapshot == payloads[0]
        artifact = await db.get(ArtifactRow, result.result_artifact_id)
        assert artifact
        output = WorkerResult.model_validate_json(ArtifactStore(cfg).read_bytes(artifact.relative_key))
        assert output.context_id == result.context_id and len(output.observations) == 2
        assert output.model_usage and output.model_usage.calls == 3 and output.model_usage.output_tokens == 60
        calls = (await db.execute(text("SELECT context_id,metric FROM ultra_worker.model_calls WHERE request_id=:id ORDER BY sequence"), {"id": first.id})).all()
        assert len(calls) == 3 and all(UUID(c.context_id) == result.context_id for c in calls)
        assert all(c.metric["status"] == "completed" and c.metric["usage_status"] == "reported" for c in calls)
        assert "isolated-fixture-key" not in json.dumps([c.metric for c in calls])
    async with database.transaction() as db:
        artifact = await db.get(ArtifactRow, ref)
        assert artifact
        artifact.availability = "missing"
    replay = await manager.delegate("native-1", value, owner="test-owner", generation=1, control_version=0)
    assert replay.id == first.id and replay.context_id == result.context_id and len(payloads) == 1
    with pytest.raises(ApiError):
        await manager.delegate("native-1", value.model_copy(update={"background": "changed"}), owner="test-owner", generation=1, control_version=0)
    # A different task gets a new context identity; no history is resumed implicitly.
    second = await manager.delegate("native-2", DelegationInput(objective="Open exploration"), owner="test-owner", generation=1, control_version=0)
    assert second.context_id != result.context_id
    assert (await manager.wait(second.id, 5)).state == "completed"


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_native_provider_stops_with_unknown_usage(database, compatible_worker, mode):
    manager = await make_manager(database, compatible_worker(hang=True, timeout=100 if mode == "timeout" else 5000))
    first = await manager.delegate("native-stop", DelegationInput(objective="synthetic test"), owner="test-owner", generation=1, control_version=0)
    if mode == "cancel":
        for _ in range(100):
            async with database.session() as db:
                count = await db.scalar(text("SELECT count(*) FROM ultra_worker.model_calls WHERE request_id=:id"), {"id": first.id})
            if count:
                break
            await asyncio.sleep(0.02)
        result = await manager.cancel(first.id)
    else:
        result = await manager.wait(first.id, 5)
    assert result.state == ("failed" if mode == "timeout" else "cancelled")
    async with database.session() as db:
        metric = await db.scalar(text("SELECT metric FROM ultra_worker.model_calls WHERE request_id=:id"), {"id": first.id})
        assert metric["input_tokens"] is None and metric["usage_status"] == "unknown"
        assert metric["failure_reason"] == ("provider_timeout" if mode == "timeout" else "provider_cancelled")


@pytest.mark.parametrize("corrupt_field", ["result", "task"])
async def test_wrong_result_context_is_rejected(database, compatible_worker, monkeypatch, corrupt_field):
    manager = await make_manager(database, compatible_worker())
    first = await manager.delegate("bad-context", DelegationInput(objective="fixture"), owner="test-owner", generation=1, control_version=0)
    original = A2AWorkerClient.get

    async def corrupt(self, task_id):
        task = await original(self, task_id)
        if task.artifacts:
            if corrupt_field == "result":
                data = MessageToDict(task.artifacts[0].parts[0].data)
                data["context_id"] = str(uuid4())
                ParseDict(data, task.artifacts[0].parts[0].data)
            else:
                task.context_id = str(uuid4())
        return task

    monkeypatch.setattr(A2AWorkerClient, "get", corrupt)
    result = await manager.wait(first.id, 5)
    assert result.state == "completed" and result.result_artifact_id is None
    assert result.diagnostic == "invalid_worker_artifact:context_identity"
