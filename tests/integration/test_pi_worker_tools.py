"""Pi Worker → existing execution kernel, real isolated DB/files and fake remote process authority."""

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.contracts.worker_tools import WorkerToolRequest
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.domain.worker_tools import WorkerToolService, tool_signature
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.execution.client import ExecutorClient
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import AgentDelegationRow, ToolOperationRow
from agent_workbench.tools.commands import run_execution_kernel
from agent_workbench.tools.runtime import ResourceLocks, Toolbox
from tests.integration.test_ultra_delegations import TOKEN, make_manager
from tests.support.pi_executor import PiFakeExecutor

pytestmark = pytest.mark.integration


async def make_case(database, settings, monkeypatch, executor, manager=None):
    cfg = settings.model_copy(update={"ultra_worker_token": TOKEN, "ultra_worker_execution_enabled": True})
    manager = manager or await make_manager(database, cfg)
    ident, task, worker, context = uuid4(), str(uuid4()), uuid4(), uuid4()
    async with database.transaction() as db:
        db.add(AgentDelegationRow(id=ident, parent_session_id=manager.session_id, parent_run_id=manager.run_id,
            parent_tool_call_id=f"parent-{ident}", request_digest="0"*64, context_id=context,
            handoff_snapshot={"execution_access": "pi_tools_v1", "evidence_access": "session_read_v1"},
            objective="synthetic", context_refs=[], authorization_snapshot={"executor_access": True,
                "tool_allowlist": ["read", "write", "edit", "bash", "grep", "find"], "owner_generation": 1, "control_version": 0},
            state="working", summary="", cancel_requested=False))

    if not hasattr(executor, "a2a_records"):
        executor.a2a_records = {}
    executor.a2a_records[ident] = {"phase": "running", "task_id": task, "worker_id": str(worker), "task": {"contextId": str(context)}}

    async def lookup(_self, request):
        return executor.a2a_records.get(request)

    monkeypatch.setattr(A2AWorkerClient, "lookup", lookup)
    service = WorkerToolService(database, cfg, cast(ExecutorClient, executor))

    async def call(tool, phase, args=None, *, call_id="call", step=0, operation_id=None, result=None):
        value = WorkerToolRequest(task_id=task, worker_id=worker, context_id=context, tool_call_id=call_id,
            tool=tool, phase=phase, arguments=args or {}, step=step, operation_id=operation_id, result=result)
        return await service.handle(ident, value, tool_signature(TOKEN, ident, value))

    return manager, ident, call


async def test_write_read_edit_and_idempotency(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    begin = await call("write", "begin", {"path": "note.txt", "content": "Alpha"})
    assert begin["workspace"].endswith(str(ident)) and begin["workspace"].startswith("/kali/")
    output = await call("write", "step", {"action": "write", "path": "note.txt", "content": "Alpha"}, step=1)
    assert output["data"]["version"]
    repeat = await call("write", "step", {"action": "write", "path": "note.txt", "content": "Alpha"}, step=1)
    assert repeat["operation_id"] == output["operation_id"] and len(fake.envelopes) == 1
    with pytest.raises(ApiError):
        await call("write", "step", {"action": "write", "path": "note.txt", "content": "Changed"}, step=1)
    saved = {"content": [{"type": "text", "text": "write complete"}]}
    await call("write", "finish", result=saved)
    cached = await call("write", "begin", {"path": "note.txt", "content": "Alpha"})
    assert cached["cached"] == saved
    await call("edit", "begin", {"path": "note.txt", "edits": [{"oldText": "Alpha", "newText": "Beta"}]}, call_id="edit")
    read = await call("edit", "step", {"action": "read", "path": "note.txt"}, call_id="edit", step=1)
    write = await call("edit", "step", {"action": "write", "path": "note.txt", "content": "Beta", "expected_version": read["data"]["version"]}, call_id="edit", step=2)
    assert write["ok"]
    conflict = await call("edit", "step", {"action": "write", "path": "note.txt", "content": "Gamma", "expected_version": read["data"]["version"]}, call_id="edit", step=3)
    assert conflict["error"] == "version_conflict"
    assert (tmp_path / str(manager.session_id) / str(ident) / "note.txt").read_text() == "Beta"
    assert all(e["run_id"] == str(manager.run_id) and e["delegation_id"] == str(ident) for e in fake.envelopes)
    async with database.session() as db:
        rows = list((await db.scalars(select(ToolOperationRow).where(ToolOperationRow.run_id == manager.run_id))).all())
        assert all(r.execution_context["agent_delegation_id"] == str(ident) for r in rows)


async def test_lost_response_queries_without_second_effect(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    _, _, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "printf worker-ok"})
    fake.lost_response = True
    first = await call("bash", "step", {"action": "bash", "command": "printf worker-ok"}, step=1)
    assert first["data"]["output"].startswith("worker-ok")
    replay = await call("bash", "step", {"action": "bash", "command": "printf worker-ok"}, step=1)
    assert replay["operation_id"] == first["operation_id"] and len(fake.envelopes) == 1


async def test_pending_cancel_and_path_boundary(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    _, ident, call = await make_case(database, settings, monkeypatch, fake)
    await call("read", "begin", {"path": "x"})
    with pytest.raises(ApiError):
        await call("read", "step", {"action": "write", "path": "x", "content": "forged"}, step=1)
    with pytest.raises(ApiError):
        await call("read", "step", {"action": "read", "path": "/etc/passwd"}, step=1)
    await call("bash", "begin", {"command": "sleep"}, call_id="slow")
    fake.hang = True
    pending = await call("bash", "step", {"action": "bash", "command": "sleep"}, call_id="slow", step=1)
    assert pending["pending"]
    before = await call("bash", "settle", {"cancel": False})
    assert not before["settled"]
    stopped = await call("bash", "settle", {"cancel": True})
    assert stopped["settled"]
    assert any(r["delegation_id"] == str(ident) for r in fake.control_syncs[-1]["revocations"])
    with pytest.raises(ApiError):
        await call("bash", "begin", {"command": "printf late"}, call_id="late")


async def test_timeout_and_unresolved_operation_keep_distinct_outcomes(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    _, _, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "sleep", "timeout": 1})
    fake.hang = True
    pending = await call("bash", "step", {"action": "bash", "command": "sleep", "timeout": 1}, step=1)
    resource = fake.resources[pending["resource_id"]]
    resource.state, resource.exit_code, resource.termination_reason = "exited", 124, "execution_timeout"
    known = await call("bash", "poll", operation_id=pending["operation_id"])
    assert known["data"]["exit_code"] == 124 and not known.get("uncertain")
    with pytest.raises(ApiError):
        await call("bash", "poll", operation_id=uuid4())
    # Losing the remote record does not authorize a second submission, even on a repeat callback.
    fake.operations.clear()
    async with database.transaction() as db:
        op = await db.get(ToolOperationRow, pending["operation_id"])
        op.job_id = None
    unknown = await call("bash", "step", {"action": "bash", "command": "sleep", "timeout": 1}, step=1)
    assert unknown["uncertain"] and len(fake.envelopes) == 1


async def test_parallel_worker_operations_have_distinct_ids_and_workspaces(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    fake.barrier_count = 3
    m1, id1, call1 = await make_case(database, settings, monkeypatch, fake)
    m2, id2, call2 = await make_case(database, settings, monkeypatch, fake, m1)
    await asyncio.gather(call1("write", "begin", {"path": "same.txt", "content": "one"}),
                         call2("write", "begin", {"path": "same.txt", "content": "two"}))
    box = Toolbox(run_id=m1.run_id, session_id=m1.session_id, owner="test-owner", owner_generation=1,
        settings=settings, db=database, scope=ScopePolicy(ScopeSnapshot()), executor=cast(ExecutorClient, fake),
        artifacts=ArtifactStore(settings), spans=SpanRecorder(), locks=ResourceLocks(settings))
    a, b, main = await asyncio.gather(
        call1("write", "step", {"action": "write", "path": "same.txt", "content": "one"}, step=1),
        call2("write", "step", {"action": "write", "path": "same.txt", "content": "two"}, step=1),
        run_execution_kernel(box, tool_call_id="main-call", tool_name="run_command", capability="command",
            action="run_command", arguments={"command": "printf main", "workspace_id": str(m1.session_id)}, wait_ms=1000))
    assert a["operation_id"] != b["operation_id"] and id1 != id2
    assert fake.peak >= 2 and m1.run_id == m2.run_id
    assert str(main.operation_id) not in {a["operation_id"], b["operation_id"]}
    assert (tmp_path/str(m1.session_id)/str(id1)/"same.txt").read_text() == "one"
    assert (tmp_path/str(m2.session_id)/str(id2)/"same.txt").read_text() == "two"
