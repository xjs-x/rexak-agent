"""Astra Stage 3 triggers: isolated PostgreSQL, deterministic transport/process authority."""

import base64
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from agent_workbench.contracts.delegation import WorkerResult
from agent_workbench.contracts.worker_evidence import WorkerEvidenceRequest
from agent_workbench.domain.worker_evidence import (
    WorkerEvidenceService,
    evidence_signature,
    visible_evidence,
)
from agent_workbench.domain.worker_tools import close_worker_aggregates, validate_output_refs
from agent_workbench.execution.client import ExecutorConnectionError, HttpExecutorClient
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    RunRow,
    ToolOperationRow,
)
from tests.integration.test_pi_worker_tools import make_case
from tests.integration.test_ultra_delegations import TOKEN
from tests.support.pi_executor import PiFakeExecutor

pytestmark = pytest.mark.integration


async def test_cancel_one_worker_uses_parent_control_and_leaves_sibling_running(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    fake.hang = True
    manager, first, one = await make_case(database, settings, monkeypatch, fake)
    _, second, two = await make_case(database, settings, monkeypatch, fake, manager)
    await one("bash", "begin", {"command": "sleep"})
    await two("bash", "begin", {"command": "sleep"})
    a = await one("bash", "step", {"action": "bash", "command": "sleep"}, step=1)
    b = await two("bash", "step", {"action": "bash", "command": "sleep"}, step=1)
    # Calling the legacy job manager is an immediate regression, even if it lies about exit.
    async def legacy(*_args):
        raise AssertionError("legacy cancellation was used")
    monkeypatch.setattr(fake, "cancel", legacy)
    assert (await one("bash", "settle", {"cancel": True}))["settled"]
    assert fake.resources[a["resource_id"]].state == "exited"
    assert fake.resources[b["resource_id"]].state == "running"
    control = fake.envelopes[-1]
    assert control["action"] == "cancel_resource" and control.get("delegation_id") is None
    assert control["arguments"]["resource_id"] == a["resource_id"]
    assert first != second


async def test_local_begin_and_pre_submission_crashes_do_not_become_remote_unknown(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "printf safe"})
    from agent_workbench.runtime.stopping import FinishGate

    async with database.session() as db:
        row = await db.get(AgentDelegationRow, ident)
        # Even before cleanup, a local formatting container cannot block Main.
        assert not await FinishGate(db)._unresolved_jobs(manager.session_id)
    await close_worker_aggregates(database, row)
    async with database.session() as db:
        aggregate = await db.scalar(select(ToolOperationRow).where(ToolOperationRow.execution_context["agent_delegation_id"].astext == str(ident)))
        assert aggregate.status == "failed" and not fake.envelopes
    # The second capability read inside the kernel fails after the leaf intent exists.
    await call("bash", "begin", {"command": "printf safe"}, call_id="cap-failure")
    original, calls = fake.capabilities, 0
    async def capabilities():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExecutorConnectionError("before submit")
        return await original()
    monkeypatch.setattr(fake, "capabilities", capabilities)
    result = await call("bash", "step", {"action": "bash", "command": "printf safe"}, call_id="cap-failure", step=1)
    assert result["not_applied"] and UUID(result["operation_id"]).int != 0
    async with database.session() as db:
        leaf = await db.get(ToolOperationRow, UUID(result["operation_id"]))
        assert leaf.status == "failed" and leaf.execution_context["submission_state"] == "not_submitted"
    assert not fake.envelopes


async def test_missing_finish_closes_local_aggregate_but_ambiguous_leaf_stays_unknown(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    _, ident, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "printf safe"})
    await call("bash", "step", {"action": "bash", "command": "printf safe"}, step=1)
    assert (await call("bash", "settle", {"cancel": False}))["settled"]
    await call("bash", "begin", {"command": "sleep"}, call_id="ambiguous")
    async def disconnected(_envelope):
        raise ExecutorConnectionError("may have accepted")
    monkeypatch.setattr(fake, "execution_submit", disconnected)
    result = await call("bash", "step", {"action": "bash", "command": "sleep"}, step=1, call_id="ambiguous")
    assert result["uncertain"]
    assert not (await call("bash", "settle", {"cancel": True}))["settled"]
    async with database.session() as db:
        operations = list((await db.scalars(select(ToolOperationRow).where(ToolOperationRow.execution_context["agent_delegation_id"].astext == str(ident)))).all())
        assert all(op.status == "failed" for op in operations if op.execution_context.get("worker_tool_aggregate"))
        unknown = [op for op in operations if op.status == "unknown"]
        assert len(unknown) == 1 and unknown[0].execution_context["submission_state"] == "may_have_submitted"


@pytest.mark.parametrize("payload,stderr,decode,truncated", [
    (b"a" * 65536, b"", "utf8", False),
    (b"a" * 65532 + b"TAIL!", b"", "utf8", True),
    ("你".encode() * 30000 + b"TAIL!", b"", "boundary_trimmed", True),
    (b"known-exit", b"\xff\xfe", "utf8", False),
], ids=["exact-64k", "over-64k-tail", "utf8-boundary-tail", "binary-stderr"])
async def test_output_range_decode_and_process_certainty(database, settings, monkeypatch, tmp_path, payload, stderr, decode, truncated):
    fake = PiFakeExecutor(tmp_path)
    _, ident, call = await make_case(database, settings, monkeypatch, fake)
    fake.hang = True
    await call("bash", "begin", {"command": "synthetic output"})
    pending = await call("bash", "step", {"action": "bash", "command": "synthetic output"}, step=1)
    resource = fake.resources[pending["resource_id"]]
    resource.state, resource.exit_code, resource.termination_reason, resource.output = "exited", 0, "natural_exit", payload
    original = fake.execution_raw_output
    async def output(resource_id, **kwargs):
        result = await original(resource_id, **kwargs)
        if kwargs["stream"] == "stderr":
            import hashlib
            result.update(data_base64=base64.b64encode(stderr).decode(), sha256=hashlib.sha256(stderr).hexdigest(),
                next_offset=len(stderr), observed_size=len(stderr), source_bytes=len(stderr))
        return result
    monkeypatch.setattr(fake, "execution_raw_output", output)
    result = await call("bash", "poll", operation_id=pending["operation_id"])
    assert not result.get("uncertain") and result["data"]["exit_code"] == 0
    assert result["data"]["truncated"] == truncated
    ref = result["output_refs"][0]
    assert ref["decode_status"] == decode and ref["observed_size"] == len(payload)
    assert ref["next_offset"] - ref["stored_offset"] == min(len(payload), 65536)
    if b"TAIL" in payload:
        assert "TAIL" in result["data"]["output"]
    if stderr:
        assert result["output_refs"][1]["decode_status"] == "invalid_utf8"
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, ident)
        model_result = WorkerResult(task_id="synthetic", status="completed", summary="low trust", observations=[],
            evidence_refs=[], unresolved_questions=[], suggested_next_steps=[], output_refs=result["output_refs"])
        assert await validate_output_refs(db, settings, row, model_result)
        artifact = await db.get(ArtifactRow, UUID(ref["artifact_id"]))
        assert artifact.truncated == truncated
        operation = await db.get(ToolOperationRow, UUID(result["operation_id"]))
        assert operation.status == "succeeded"
    # Routine Executor reconciliation must not erase validated output source metadata.
    from agent_workbench.persistence.execution_projection import sync_resource_operation
    from agent_workbench.persistence.tables import ExecutionResourceRow
    async with database.transaction() as db:
        resource_row = await db.get(ExecutionResourceRow, UUID(pending["resource_id"]))
        await sync_resource_operation(db, resource_row)
        assert await validate_output_refs(db, settings, row, model_result)


async def test_internal_base64_is_not_shared_and_output_refs_cannot_cross_workers(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, first, one = await make_case(database, settings, monkeypatch, fake)
    _, second, _two = await make_case(database, settings, monkeypatch, fake, manager)
    root = tmp_path / str(manager.session_id) / str(first)
    root.mkdir(parents=True)
    secret = "password=synthetic-secret-DO-NOT-SHARE"
    (root / "secret.txt").write_text(secret, encoding="utf-8")
    await one("read", "begin", {"path": "secret.txt"})
    result = await one("read", "step", {"action": "read", "path": "secret.txt"}, step=1)
    assert base64.b64decode(result["data"]["data_base64"]).decode() == secret
    async with database.transaction() as db:
        # Pre-repair artifacts had neither this tag nor a special MIME type.
        legacy = await db.get(ArtifactRow, UUID(result["artifact_id"]))
        legacy.captured_by_identity, legacy.media_type = None, "text/plain"
        row = await db.get(AgentDelegationRow, second)
        row.authorization_snapshot = {**row.authorization_snapshot, "tool_allowlist": ["list_evidence", "read_evidence"]}
        worker, context = UUID(fake.a2a_records[second]["worker_id"]), row.context_id
        visible = list((await db.scalars(select(ArtifactRow.id).where(*visible_evidence(manager.session_id)))).all())
        assert UUID(result["artifact_id"]) not in visible
    request = WorkerEvidenceRequest(task_id=fake.a2a_records[second]["task_id"], worker_id=worker, context_id=context,
        tool_call_id="forbidden-read", action="read", artifact_id=UUID(result["artifact_id"]))
    service = WorkerEvidenceService(database, settings.model_copy(update={"ultra_worker_token": TOKEN}))
    denied = await service.handle(second, request, evidence_signature(TOKEN, second, request))
    assert not denied["ok"] and denied["error"] == "evidence_not_available"
    forged = WorkerResult(task_id="synthetic", status="completed", summary="low trust", observations=[],
        evidence_refs=[], unresolved_questions=[], suggested_next_steps=[], output_refs=result["output_refs"])
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, second)
        assert not await validate_output_refs(db, settings, row, forged)


async def test_failure_cleanup_is_not_user_cancel_and_helper_conflict_is_failed(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    _, ident, call = await make_case(database, settings, monkeypatch, fake)
    await call("edit", "begin", {"path": "note.txt"})
    result = await call("edit", "step", {"action": "write", "path": "note.txt", "content": "new", "expected_version": "0"*64}, step=1)
    assert not result["ok"] and result["error"] == "version_conflict"
    assert (await call("bash", "settle", {"cancel": True, "reason": "failure"}))["settled"]
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, ident)
        operation = await db.get(ToolOperationRow, UUID(result["operation_id"]))
        assert not row.cancel_requested and row.authorization_snapshot["execution_revoked"]
        assert operation.status == "failed"


async def test_collection_gaps_and_read_failure_do_not_change_known_exit(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    _, _, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "printf safe"})
    original = fake.execution_raw_output
    async def gaps(resource_id, **kwargs):
        result = await original(resource_id, **kwargs)
        if kwargs["stream"] == "stdout":
            size = result["observed_size"]
            result.update(source_bytes=size + 100, missing_ranges=[[size, size+100]])
        return result
    monkeypatch.setattr(fake, "execution_raw_output", gaps)
    result = await call("bash", "step", {"action": "bash", "command": "printf safe"}, step=1)
    assert result["data"]["truncated"] and result["output_refs"][0]["missing_ranges"]
    async def failed(*args, **kwargs):
        raise ExecutorConnectionError("log unavailable after known exit")
    monkeypatch.setattr(fake, "execution_raw_output", failed)
    result = await call("bash", "poll", operation_id=result["operation_id"])
    assert result["error"].startswith("output_unavailable") and result["process_status"] == "succeeded"
    assert not result.get("uncertain")


async def test_http_accepted_response_loss_never_reposts_after_fence_change(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, _ident, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "printf safe"})
    real_submit, posts = fake.execution_submit, 0
    async def transport(request):
        nonlocal posts
        import json
        envelope = json.loads(request.content)
        posts += 1
        await real_submit(envelope)
        # The first physical POST was accepted, then the response and its authority are lost.
        async with database.transaction() as db:
            run = await db.get(RunRow, manager.run_id)
            run.control_version += 1
        raise httpx.ReadError("response lost after acceptance", request=request)
    http = HttpExecutorClient(None, settings=settings)  # type: ignore[arg-type]
    http._control = httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="http://fixture")
    async def prohibited_rebind():
        raise AssertionError("mutation must not be rebound and retried")
    monkeypatch.setattr(http, "_rebind", prohibited_rebind)
    monkeypatch.setattr(fake, "execution_submit", http.execution_submit)
    try:
        result = await call("bash", "step", {"action": "bash", "command": "printf safe"}, step=1)
        assert result["data"]["output"].startswith("worker-ok") and posts == 1
        assert len(fake.envelopes) == 1 and not result.get("not_applied")
    finally:
        await http.close()


@pytest.mark.parametrize("accepted", [True, False])
async def test_cancel_response_loss_is_reconciled_without_second_post(database, settings, monkeypatch, tmp_path, accepted):
    fake = PiFakeExecutor(tmp_path)
    fake.hang = True
    _, _, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "sleep"})
    pending = await call("bash", "step", {"action": "bash", "command": "sleep"}, step=1)
    submit, sends = fake.execution_submit, 0
    async def lost(envelope):
        nonlocal sends
        assert envelope["action"] == "cancel_resource"
        sends += 1
        if accepted:
            await submit(envelope)
        raise ExecutorConnectionError("cancel response lost")
    monkeypatch.setattr(fake, "execution_submit", lost)
    assert (await call("bash", "settle", {"cancel": True}))["settled"] == accepted
    assert (await call("bash", "settle", {"cancel": True}))["settled"] == accepted
    assert sends == 1
    if not accepted:
        state = fake.resources[pending["resource_id"]]
        state.state, state.exit_code, state.termination_reason = "exited", 0, "natural_exit"
        assert (await call("bash", "settle", {"cancel": True}))["settled"]
