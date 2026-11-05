"""真实 ASGI 原始字节、SQLite 台账与文件往返，无模型或在线目标。"""

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from workbench_executor.execution_api import create_router
from workbench_executor.filesystem import WorkspaceFileSystem
from workbench_executor.ledger import Ledger

from agent_workbench.contracts.execution_runtime import InputFileArguments, execution_request_digest
from agent_workbench.execution.client import ExecutorRejected, HttpExecutorClient


def flask_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app/routes.py", "from .security import is_admin\n# /admin route\n")
        archive.writestr("app/security.py", "def is_admin(user):\n    return user.is_admin\n")
        archive.writestr("app/merge.py", "def merge(a, b):\n    return {**a, **b}\n")
        archive.writestr("program.bin", b"\x7fELF\x00\xff")
    return buffer.getvalue()


@pytest.fixture
async def delivery(tmp_path):
    import asyncio

    ledger = Ledger(tmp_path / "ledger.db")
    await ledger.open()
    session, run = str(uuid4()), str(uuid4())
    await ledger.sync_fence(session_id=session, run_id=run, owner="test", owner_generation=1,
                            control_version=0, expected_seq=None, revocations=[])
    root = tmp_path / "w"
    if os.name == "nt":
        root = Path("\\\\?\\" + str(root))
    state = SimpleNamespace(ledger=ledger, settings=SimpleNamespace(workspace_root=root),
        execution_admission_lock=asyncio.Lock(), execution_available=True,
        command_runtime=SimpleNamespace(_ensure_disk_headroom=lambda: None))
    app = FastAPI()
    app.include_router(create_router(lambda: state, lambda: None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://executor") as client:
        yield state, client, session, run
    await ledger.close()


def envelope(session, run, content, worker=None, **overrides) -> dict[str, Any]:
    arguments = InputFileArguments(attachment_id=uuid4(), filename="challenge.zip",
        sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content), worker_workspace_id=worker).model_dump(mode="json")
    arguments.update(overrides)
    return {"operation_id": str(uuid4()), "session_id": session, "run_id": run,
        "action": "materialize_attachment", "arguments": arguments, "tool_call_id": "call-1", "owner_generation": 1,
        "dispatch_control_version": 0, "session_control_seq": 1,
        "delegation_id": str(worker) if worker else None, "delegation_revision": 1 if worker else None,
        "request_digest": execution_request_digest("materialize_attachment", arguments)}


async def send(client, env, content):
    return await client.put(f"/execution/v1/input-files/{env['operation_id']}", content=content,
        headers={"X-Execution-Envelope": json.dumps(env)})


async def test_main_and_two_workers_use_original_zip(delivery, tmp_path):
    state, http, session, run = delivery
    raw = flask_zip()
    paths = []
    for worker in (None, uuid4(), uuid4()):
        env = envelope(session, run, raw, worker)
        source = tmp_path / "original.zip"
        source.write_bytes(raw)
        # Production client streams the file, never base64 JSON.
        client = object.__new__(HttpExecutorClient)
        client._data = http
        receipt = await client.execution_input_file(env, source)
        info = receipt["input_file"]
        root = state.settings.workspace_root / session
        if worker:
            root = root / ".rexak" / "workers" / str(worker)
        actual = root / info["relative_path"]
        paths.append(actual)
        assert actual.read_bytes() == raw
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == info["sha256"]
        with zipfile.ZipFile(actual) as archive:
            assert "app/routes.py" in archive.namelist()
            archive.extractall(root / "source")  # This fixture has only fixed trusted member names.
        fs = WorkspaceFileSystem(root)
        assert "is_admin" in (root / "source/app/routes.py").read_text()
        assert len([p for p in (root / "source").rglob("*.py") if "is_admin" in p.read_text()]) == 2
        assert fs.resolve("source/app/security.py").is_file()
        assert (root / "source/program.bin").read_bytes() == b"\x7fELF\x00\xff"
        again = await client.execution_input_file(env, source)
        assert again["deduplicated"]
        assert (await state.ledger.get_operation(env["operation_id"])).status == "succeeded"
    assert len(set(paths)) == 3


@pytest.mark.parametrize("case", ["hash", "short", "oversize", "filename", "worker"])
async def test_invalid_input_does_not_publish(delivery, case):
    state, client, session, run = delivery
    raw = flask_zip()
    worker = uuid4() if case == "worker" else None
    env = envelope(session, run, raw, worker)
    if case == "hash":
        raw = bytes([raw[0] ^ 1]) + raw[1:]
    if case == "short":
        raw = raw[:-1]
    if case == "oversize":
        raw += b"x"
    if case == "filename":
        env["arguments"]["filename"] = "../../evil.zip"
        # Filenames are metadata: the server sanitizes even hostile names into one basename.
        env["request_digest"] = execution_request_digest(env["action"], env["arguments"])
        result = await send(client, env, raw)
        assert result.status_code == 200
        assert ".." not in result.json()["input_file"]["relative_path"].split("/")
        return
    if case == "worker":
        env["delegation_id"] = str(uuid4())
    result = await send(client, env, raw)
    assert result.status_code == 422
    assert not list(state.settings.workspace_root.rglob("challenge.zip"))
    assert not list(state.settings.workspace_root.rglob(".incoming-*"))


async def test_transport_failure_removes_staged_file(delivery):
    state, client, session, run = delivery
    raw = flask_zip()
    env = envelope(session, run, raw)

    async def interrupted():
        yield raw[:20]
        raise httpx.ReadError("interrupted")

    with pytest.raises(httpx.ReadError):
        await send(client, env, interrupted())
    assert not list(state.settings.workspace_root.rglob(".incoming-*"))
    assert not list(state.settings.workspace_root.rglob("challenge.zip"))


async def test_fence_changed_during_stream_does_not_publish(delivery):
    state, client, session, run = delivery
    raw = flask_zip()
    env = envelope(session, run, raw)

    async def changed():
        yield raw[:20]
        await state.ledger.sync_fence(session_id=session, run_id=run, owner="test", owner_generation=1,
                                      control_version=1, expected_seq=None, revocations=[])
        yield raw[20:]

    from workbench_executor.command_runtime import RuntimeRejected

    with pytest.raises(RuntimeRejected, match="旧控制版本"):
        await send(client, env, changed())
    assert not list(state.settings.workspace_root.rglob("challenge.zip"))


async def test_destination_modified_is_never_overwritten(delivery):
    state, http, session, run = delivery
    raw = flask_zip()
    env = envelope(session, run, raw)
    result = await send(http, env, raw)
    target = state.settings.workspace_root / session / result.json()["input_file"]["relative_path"]
    target.write_bytes(b"user work")
    result = await send(http, env, raw)
    assert result.status_code == 422
    assert target.read_bytes() == b"user work"


async def test_client_explicit_rejection(delivery, tmp_path):
    _, http, session, run = delivery
    source = tmp_path / "file"
    source.write_bytes(b"changed")
    client = object.__new__(HttpExecutorClient)
    client._data = http
    with pytest.raises(ExecutorRejected):
        await client.execution_input_file(envelope(session, run, b"original"), source)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "C:/drive", "a\\escape", ".incomplete"])
def test_bounded_shell_recipe_rejects_unsafe_members(tmp_path, name):
    from tests.support.extract_attachment import extract

    source = tmp_path / "input.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(name, "no write")
    if "\\" in name:
        source.write_bytes(source.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    with pytest.raises(ValueError, match="unsafe"):
        extract(source, tmp_path / "output")
    assert (tmp_path / "output/.incomplete").exists()
    assert not (tmp_path / "escape").exists()


def test_bounded_recipe_limits_links_and_existing_output(tmp_path):
    from tests.support.extract_attachment import extract

    source = tmp_path / "input.zip"
    source.write_bytes(flask_zip())
    with pytest.raises(ValueError, match="limit"):
        extract(source, tmp_path / "limited", max_bytes=10)
    with pytest.raises(FileExistsError):
        extract(source, tmp_path / "limited")
    with zipfile.ZipFile(source, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "../outside")
    with pytest.raises(ValueError, match="unsafe"):
        extract(source, tmp_path / "link-output")
