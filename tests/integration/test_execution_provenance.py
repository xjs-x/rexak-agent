"""Real isolated DB/CAS, fake Executor, no model or target access."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.tables import ArtifactRow, ToolOperationRow
from agent_workbench.tools.evidence import read_artifact
from tests.integration.test_pi_worker_tools import make_case
from tests.support.pi_executor import PiFakeExecutor

pytestmark = pytest.mark.integration


def runtime(database, settings, manager):
    return SimpleNamespace(context=SimpleNamespace(toolbox=SimpleNamespace(
        db=database, session_id=manager.session_id, artifacts=ArtifactStore(settings))))


async def read(database, settings, manager, artifact_id, **kwargs):
    return json.loads(await read_artifact.coroutine(
        runtime=runtime(database, settings, manager), artifact_id=artifact_id, **kwargs))


async def execute(call, command, call_id="call"):
    await call("bash", "begin", {"command": command}, call_id=call_id)
    return await call("bash", "step", {"action": "bash", "command": command}, step=1, call_id=call_id)


async def add_event(database, ident, call_id, command, sequence):
    async with database.transaction() as db:
        await db.execute(text("""INSERT INTO ultra_worker.requests(request_id,digest,task_id,worker_id,payload)
            VALUES (:id,'test',:task,:worker,'{}') ON CONFLICT DO NOTHING"""),
            {"id": ident, "task": str(ident), "worker": uuid4()})
        message = {"content": [{"type": "toolCall", "id": call_id, "name": "bash", "arguments": {"command": command}}]}
        await db.execute(text("""INSERT INTO ultra_worker.visible_events(request_id,sequence,role,message,sha256)
            VALUES (:id,:seq,'assistant',CAST(:message AS jsonb),'test')"""),
            {"id": ident, "seq": sequence, "message": json.dumps(message)})


async def test_normal_command_and_request_replay(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    output = await execute(call, "python3 --version")
    result = await read(database, settings, manager, output["artifact_id"])
    p = result["data"]["execution_provenance"]
    assert result["data"]["text"] == "worker-ok\n"
    assert p["command"] == "python3 --version" and p["provenance_status"] == "complete"
    assert p["operation_id"] == output["operation_id"]
    assert p["job_id"] == output["output_refs"][0]["resource_id"]
    assert p["exit_code"] == 0 and p["status"] == "succeeded"
    assert p["delegation_id"] == str(ident) and p["cwd"].endswith(str(ident))
    again = await call("bash", "step", {"action": "bash", "command": "python3 --version"}, step=1)
    assert again["operation_id"] == p["operation_id"] and len(fake.envelopes) == 1
    request = await read(database, settings, manager, p["request_ref"]["artifact_id"])
    assert json.loads(request["data"]["text"])["arguments"]["command"] == "python3 --version"


async def test_script_historical_write_and_no_live_file_read(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    creation = "cd /tmp/et && cat > run.sh <<'EOF'\nOPENSSL_MODULES=/nonexistent ./easytl\nEOF\n"
    await execute(call, creation, "write-script")
    await add_event(database, ident, "write-script", creation, 1)
    command = "cd /tmp/et && ./run.sh python3 cli.py"
    output = await execute(call, command, "run-script")
    await add_event(database, ident, "run-script", command, 3)
    # A current, different file is irrelevant; provenance must never read it.
    (tmp_path / "run.sh").write_text("UNRELATED CURRENT FILE", encoding="utf-8")
    p = (await read(database, settings, manager, output["artifact_id"]))["data"]["execution_provenance"]
    assert p["command"] == command and p["provenance_status"] == "partial"
    ref = next(r for r in p["reproduction_refs"] if r["path"] == "/tmp/et/run.sh")
    assert "OPENSSL_MODULES=/nonexistent" in ref["write_preview"]
    assert ref["version_status"] == "historical_write_not_execution_snapshot"
    assert ref["sequence"] == 1 and "UNRELATED" not in json.dumps(p)
    # This implementation does not collect execution-time script snapshots.
    assert any(r["version_status"] == "execution_version_unavailable" for r in p["reproduction_refs"])


async def test_workers_and_sessions_are_isolated(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, id1, call1 = await make_case(database, settings, monkeypatch, fake)
    _, id2, call2 = await make_case(database, settings, monkeypatch, fake, manager)
    a = await execute(call1, "echo first")
    b = await execute(call2, "echo second")
    p = (await read(database, settings, manager, a["artifact_id"]))["data"]["execution_provenance"]
    assert p["delegation_id"] == str(id1) and str(id2) not in json.dumps(p)
    assert p["command"] == "echo first" and b["operation_id"] not in json.dumps(p)
    other, _, _ = await make_case(database, settings, monkeypatch, fake)
    denied = await read(database, settings, other, a["artifact_id"])
    assert denied["status"] == "failed" and "execution_provenance" not in denied.get("data", {})
    # Even a poisoned same-session artifact cannot traverse a foreign Operation.
    async with database.transaction() as db:
        row = await db.get(ArtifactRow, UUID(a["artifact_id"]))
        row.session_id = other.session_id
    denied_source = await read(database, settings, other, a["artifact_id"])
    assert denied_source["data"]["execution_provenance"]["provenance_status"] == "unavailable"


@pytest.mark.parametrize("missing", ["operation", "arguments", "request-file"])
async def test_missing_history_keeps_output_readable(database, settings, monkeypatch, tmp_path, missing):
    fake = PiFakeExecutor(tmp_path)
    manager, _, call = await make_case(database, settings, monkeypatch, fake)
    output = await execute(call, "echo historic")
    async with database.transaction() as db:
        artifact = await db.get(ArtifactRow, UUID(output["artifact_id"]))
        operation = await db.get(ToolOperationRow, UUID(output["operation_id"]))
        if missing == "operation":
            artifact.source_operation_id = uuid4()
        elif missing == "arguments":
            operation.execution_context = {k: v for k, v in operation.execution_context.items() if k != "execution_request_artifact_id"}
        else:
            request = await db.get(ArtifactRow, UUID(operation.execution_context["execution_request_artifact_id"]))
            request.relative_key = "artifacts/missing"
    result = await read(database, settings, manager, output["artifact_id"])
    assert result["data"]["text"] == "worker-ok\n"
    assert result["data"]["execution_provenance"]["provenance_status"] == "unavailable"


async def test_failure_truncation_and_redaction(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, _, call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "sleep"})
    fake.hang = True
    pending = await call("bash", "step", {"action": "bash", "command": "sleep"}, step=1)
    resource = fake.resources[pending["resource_id"]]
    resource.state, resource.exit_code, resource.termination_reason = "exited", 17, "natural_exit"
    resource.output = b"x" * 70000
    output = await call("bash", "poll", operation_id=pending["operation_id"])
    result = await read(database, settings, manager, output["artifact_id"], limit=12)
    p = result["data"]["execution_provenance"]
    assert result["data"]["truncated"] and p["output_capture_truncated"]
    assert p["exit_code"] == 17 and p["status"] == "failed"
    fake.hang = False
    secret = "OPENSSL_MODULES=/nonexistent API_KEY='secret with spaces' python3 --version"
    output = await execute(call, secret, "secret")
    result = await read(database, settings, manager, output["artifact_id"])
    encoded = json.dumps(result)
    assert "secret with spaces" not in encoded and "OPENSSL_MODULES=/nonexistent" in encoded
    assert result["data"]["execution_provenance"]["provenance_status"] == "partial"


async def test_legacy_command_uses_exact_tool_call_namespace(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    output = await execute(call, "python3 --version")
    await add_event(database, ident, "different-call", "echo wrong", 1)
    await add_event(database, ident, "call", "python3 --version", 2)
    async with database.transaction() as db:
        operation = await db.get(ToolOperationRow, UUID(output["operation_id"]))
        operation.execution_context = {k: v for k, v in operation.execution_context.items() if k != "execution_request_artifact_id"}
    p = (await read(database, settings, manager, output["artifact_id"]))["data"]["execution_provenance"]
    assert p["command"] == "python3 --version" and p["tool_call_ref"]["sequence"] == 2


async def test_ordinary_artifact_shape_unchanged(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, _, call = await make_case(database, settings, monkeypatch, fake)
    output = await execute(call, "echo ordinary")
    async with database.transaction() as db:
        row = await db.get(ArtifactRow, UUID(output["artifact_id"]))
        row.source_operation_id, row.captured_by_identity = None, "main"
    result = await read(database, settings, manager, output["artifact_id"])
    assert "execution_provenance" not in result["data"] and result["data"]["text"] == "worker-ok\n"


async def test_aggregate_result_resolves_real_job(database, settings, monkeypatch, tmp_path):
    fake = PiFakeExecutor(tmp_path)
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    command = "cd /tmp/et && cat > run.sh <<'EOF'\nOPENSSL_MODULES=/nonexistent ./easytl\nEOF\n./run.sh python3 cli.py"
    output = await execute(call, command)
    await add_event(database, ident, "call", command, 1)
    await call("bash", "finish", result={"content": [{"type": "text", "text": "handshake ok"}]})
    from sqlalchemy import select

    async with database.session() as db:
        aggregate = await db.scalar(select(ToolOperationRow).where(
            ToolOperationRow.run_id == manager.run_id, ToolOperationRow.tool_name == "pi.bash"))
        artifact_id = aggregate.result_artifact_ids[0]
        parent_id = str(aggregate.operation_id)
    p = (await read(database, settings, manager, artifact_id))["data"]["execution_provenance"]
    assert p["worker_operation_id"] == parent_id and p["operation_id"] == output["operation_id"]
    assert p["job_id"] == output["output_refs"][0]["resource_id"] and p["exit_code"] == 0
    assert "OPENSSL_MODULES=/nonexistent" in p["command"] and p["reproduction_refs"]
    assert p["provenance_status"] == "partial"


async def test_long_command_retains_request_reference(database, settings, monkeypatch, tmp_path):
    manager, _, call = await make_case(database, settings, monkeypatch, PiFakeExecutor(tmp_path))
    command = "echo " + "x" * 10000
    output = await execute(call, command)
    p = (await read(database, settings, manager, output["artifact_id"]))["data"]["execution_provenance"]
    assert p["command_truncated"] and len(p["command"]) == 6000 and p["provenance_status"] == "partial"
    request = await read(database, settings, manager, p["request_ref"]["artifact_id"])
    assert json.loads(request["data"]["text"])["arguments"]["command"] == command
