"""Session evidence boundary, durable receipts and real Python/Node callback path."""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.delegation import DelegationInput, WorkerResult
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.worker_evidence import WorkerEvidenceRequest
from agent_workbench.domain.worker_evidence import WorkerEvidenceService, evidence_signature
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    DelegationEvidenceReadRow,
    ModelCallRow,
    RunRow,
)
from tests.integration.test_ultra_delegations import TOKEN, make_manager

pytestmark = pytest.mark.integration


async def artifact(database, settings, session_id, raw=b"historical evidence", summary="fixture", **extra):
    stored = ArtifactStore(settings).write_bytes(raw, media_type="text/plain")
    async with database.transaction() as db:
        row = await ArtifactRepository(db).create(sha256=stored.sha256, byte_size=stored.byte_size,
            relative_key=stored.relative_key, media_type=stored.media_type, session_id=session_id,
            source_operation_id=None, sensitivity="internal", availability="available",
            captured_at=stored.captured_at, summary=summary, **extra)
        return row.id


@pytest.fixture
async def evidence_case(database, settings, monkeypatch):
    cfg = settings.model_copy(update={"ultra_worker_token": TOKEN})
    manager = await make_manager(database, cfg)
    delegation, task_id, worker_id, context_id = uuid4(), str(uuid4()), uuid4(), uuid4()
    async with database.transaction() as db:
        db.add(AgentDelegationRow(id=delegation, parent_session_id=manager.session_id, parent_run_id=manager.run_id,
            parent_tool_call_id="parent-call", request_digest="0" * 64, context_id=context_id,
            handoff_snapshot={"evidence_access": "session_read_v1", "context_sources": []},
            objective="fixture", context_refs=[], authorization_snapshot={"owner_generation": 1, "control_version": 0,
                "tool_allowlist": ["list_evidence", "read_evidence"]},
            state="submitting", summary="", cancel_requested=False))

    async def lookup(_self, _id):
        return {"phase": "running", "cancel_requested": False, "task_id": task_id, "worker_id": str(worker_id), "task": {"contextId": str(context_id)}}

    monkeypatch.setattr(A2AWorkerClient, "lookup", lookup)
    service = WorkerEvidenceService(database, cfg)

    async def call(action: Literal["list", "read"] = "read", **kwargs):
        value = WorkerEvidenceRequest(task_id=task_id, worker_id=worker_id, context_id=context_id,
            tool_call_id=kwargs.pop("tool_call_id", str(uuid4())), action=action, **kwargs)
        return await service.handle(delegation, value, evidence_signature(TOKEN, delegation, value))

    return manager, service, call, delegation, cfg


async def test_paging_redaction_receipts_and_idempotency(database, evidence_case):
    manager, _, call, delegation, cfg = evidence_case
    ref = await artifact(database, cfg, manager.session_id,
        b'{"nested":{"password":"never-expose-this-long-secret"},"message":"historical progress"}')
    parts, offset = [], 0
    while True:
        result = await call(artifact_id=ref, offset=offset, limit=7)
        assert result["ok"]
        data = result["data"]
        assert data["returned_bytes"] <= 7 and data["offset"] == offset
        parts.append(data["text"])
        offset = data["next_offset"]
        if not data["truncated"]:
            break
    assert "never-expose" not in "".join(parts) and "<redacted>" in "".join(parts)
    first = await call(artifact_id=ref, tool_call_id="same-call")
    duplicate = await call(artifact_id=ref, tool_call_id="same-call")
    assert duplicate == first and first["read_ref"]["artifact_id"] == str(ref)
    with pytest.raises(ApiError):
        await call(artifact_id=ref, offset=7, tool_call_id="same-call")
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, delegation)
        assert row.a2a_task_id is not None  # callback before submit acknowledgement safely binds identity.
        assert await db.scalar(select(func.count()).select_from(DelegationEvidenceReadRow).where(
            DelegationEvidenceReadRow.delegation_id == delegation, DelegationEvidenceReadRow.tool_call_id == "same-call")) == 1
    listing = await call(action="list")
    assert [item["artifact_id"] for item in listing["artifacts"]] == [str(ref)]


async def test_session_boundary_changed_missing_binary_and_utf8(database, evidence_case):
    manager, _, call, _, cfg = evidence_case
    other = await make_manager(database, cfg)
    foreign = await artifact(database, cfg, other.session_id)
    assert (await call(artifact_id=foreign))["error"] == "evidence_not_available"
    assert not (await call(action="list"))["artifacts"]
    ref = await artifact(database, cfg, manager.session_id, "中文历史证据".encode())
    assert (await call(artifact_id=ref, offset=1))["error"] == "invalid_view_byte_offset"
    good = await call(artifact_id=ref, limit=4)
    assert good["data"]["text"] == "中" and good["data"]["next_offset"] == 3
    assert (await call(artifact_id=ref, expected_sha256="0" * 64))["error"] == "source_reference_changed"
    async with database.session() as db:
        row = await db.get(ArtifactRow, ref)
        key = row.relative_key
    ArtifactStore(cfg).resolve(key).write_bytes(b"tampered")
    assert (await call(artifact_id=ref))["error"] == "source_integrity_mismatch"
    ArtifactStore(cfg).resolve(key).unlink()
    assert (await call(artifact_id=ref))["error"] == "evidence_read_failed"
    invalid = await artifact(database, cfg, manager.session_id, b"\xff\xfe")
    assert (await call(artifact_id=invalid))["error"] == "invalid_utf8_source"


@pytest.mark.parametrize("stop", ["cancel", "pause", "lease", "expiry", "generation", "control", "terminal", "permission"])
async def test_access_revoked_by_parent_control(database, evidence_case, stop):
    manager, _, call, delegation, _ = evidence_case
    await call(action="list", tool_call_id="cached-before-stop")
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        row = await db.get(AgentDelegationRow, delegation)
        if stop == "cancel":
            row.cancel_requested = True
        elif stop == "pause":
            run.status = "paused"
        elif stop == "lease":
            run.lease_until = None
        elif stop == "expiry":
            run.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif stop == "generation":
            run.owner_generation += 1
        elif stop == "terminal":
            row.state = "completed"
        elif stop == "permission":
            row.authorization_snapshot = {**row.authorization_snapshot, "tool_allowlist": []}
        else:
            run.control_version += 1
    with pytest.raises(ApiError):
        await call(action="list", tool_call_id="cached-before-stop")


async def test_catalog_paging_secret_and_model_archives_are_not_evidence(database, evidence_case):
    manager, _, call, _, cfg = evidence_case
    for index in range(23):
        await artifact(database, cfg, manager.session_id, f"data-{index}".encode())
    hidden = await artifact(database, cfg, manager.session_id, b"private")
    archive = await artifact(database, cfg, manager.session_id, b"private model archive")
    async with database.transaction() as db:
        source = await db.get(ArtifactRow, hidden)
        source.sensitivity = "secret"
        db.add(ModelCallRow(model_call_id=uuid4(), logical_call_id=uuid4(), run_id=manager.run_id,
            session_id=manager.session_id, phase="fixture", provider_id="mock", model_name="mock", response_artifact_id=archive))
    one = await call(action="list")
    two = await call(action="list", cursor=UUID(one["next_cursor"]))
    assert len(one["artifacts"]) == 20 and len(two["artifacts"]) == 3 and two["next_cursor"] is None
    assert (await call(artifact_id=hidden))["error"] == "evidence_not_available"
    assert (await call(artifact_id=archive))["error"] == "evidence_not_available"
    ref = UUID(one["artifacts"][0]["artifact_id"])
    async with database.transaction() as db:
        source = await db.get(ArtifactRow, ref)
        source.media_type = "application/octet-stream"
    assert (await call(artifact_id=ref))["error"] == "unsupported_media_type"
    async with database.transaction() as db:
        source = await db.get(ArtifactRow, ref)
        source.media_type, source.relative_key = "text/plain", "../outside-evidence-root"
    assert (await call(artifact_id=ref))["error"] == "evidence_read_failed"


async def test_authentication_and_forged_evidence_rejected(database, evidence_case):
    manager, service, call, delegation, cfg = evidence_case
    ref = await artifact(database, cfg, manager.session_id)
    value = WorkerEvidenceRequest(task_id="forged", worker_id=uuid4(), context_id=uuid4(), tool_call_id="x", action="list")
    with pytest.raises(ApiError):
        await service.handle(delegation, value, "invalid")
    with pytest.raises(ApiError):
        await service.handle(delegation, value, evidence_signature(TOKEN, delegation, value))
    result = await call(artifact_id=ref)
    from agent_workbench.domain.worker_evidence import validate_result_evidence

    returned = WorkerResult(task_id="fixture", status="completed", summary="analysis", observations=[],
        evidence_refs=[str(ref)], evidence_reads=[result["read_ref"]], unresolved_questions=[], suggested_next_steps=[])
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, delegation)
        assert await validate_result_evidence(db, cfg, row, returned)
        returned.evidence_reads[0].returned_bytes += 1
        assert not await validate_result_evidence(db, cfg, row, returned)
        returned.evidence_reads = []
        assert not await validate_result_evidence(db, cfg, row, returned)


def free_port():
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


@pytest.fixture
async def evidence_runtime(settings, tmp_path):
    node = shutil.which("node")
    assert node
    api_port, worker_port = free_port(), free_port()
    cfg = settings.model_copy(update={"ultra_worker_url": f"http://127.0.0.1:{worker_port}", "ultra_worker_token": TOKEN})
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}}
    env.update(WORKBENCH_DATABASE_URL=settings.database_url, WORKBENCH_DATA_ROOT=str(settings.data_root),
        WORKBENCH_ULTRA_WORKER_URL=cfg.ultra_worker_url, WORKBENCH_ULTRA_WORKER_TOKEN=TOKEN,
        WORKBENCH_OWNER_ID=str(settings.owner_id), WORKBENCH_API_PORT=str(api_port),
        REXAK_WORKER_DATABASE_URL=make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False),
        REXAK_WORKER_TOKEN=TOKEN, FIXTURE_PORT=str(worker_port), REXAK_WORKER_CONTROL_URL=f"http://127.0.0.1:{api_port}", PYTHONIOENCODING="utf-8")
    with (tmp_path / "api.log").open("wb") as api_log, (tmp_path / "worker.log").open("wb") as worker_log:
        api = subprocess.Popen([sys.executable, "-m", "agent_workbench.cli", "api", "--host", "127.0.0.1", "--port", str(api_port)], env=env, cwd=REPO_ROOT, stdout=api_log, stderr=api_log)
        worker = subprocess.Popen([node, "--import", "tsx", "test/evidence-a2a-server.ts"], env=env, cwd=REPO_ROOT / "worker", stdout=worker_log, stderr=worker_log)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                for url in [f"http://127.0.0.1:{api_port}/health", cfg.ultra_worker_url + "/health"]:
                    for _ in range(200):
                        assert api.poll() is None and worker.poll() is None
                        try:
                            result = await client.get(url, headers={"Authorization": "Bearer " + TOKEN})
                            if result.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        raise AssertionError("Evidence runtime startup timeout")
            yield cfg
        finally:
            for process in (worker, api):
                process.terminate()
                process.wait(timeout=10)


async def test_worker_autonomously_reads_historical_chain_over_a2a(database, evidence_runtime):
    cfg = evidence_runtime
    manager = await make_manager(database, cfg)
    second = await artifact(database, cfg, manager.session_id, b"PART=99\n", "chain-second")
    first = await artifact(database, cfg, manager.session_id, ("x" * 55 + f"\nPART=73\nNEXT={second}\n").encode(), "chain-start")
    record = await manager.delegate("evidence-chain", DelegationInput(objective="Find the two-part code from historical evidence", context_refs=[first]),
        owner="test-owner", generation=1, control_version=0)
    result = await manager.wait(record.id, 15)
    assert result.state == "completed", result.model_dump()
    assert "7399" in result.summary, result.model_dump()
    async with database.session() as db:
        output_artifact = await db.get(ArtifactRow, result.result_artifact_id)
        output = WorkerResult.model_validate_json(ArtifactStore(cfg).read_bytes(output_artifact.relative_key))
        assert set(output.evidence_refs) == {str(first), str(second)}
        assert len(output.evidence_reads) == 2
        receipts = list((await db.scalars(select(DelegationEvidenceReadRow).where(DelegationEvidenceReadRow.delegation_id == record.id))).all())
        assert len(receipts) >= 4  # directory + multiple pages + discovered, non-recommended evidence.
        assert all(receipt.response_artifact_id for receipt in receipts)
        assert await db.scalar(select(func.count()).select_from(ArtifactRow).where(ArtifactRow.session_id == manager.session_id)) >= 7
    # Parent completed results remain model analysis, no Finding/Verification was created by this path.
