"""显式启用的真实 Kali 文件验收；独占测试数据库，无付费模型或网络靶场。"""

import asyncio
import base64
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.contracts.worker_tools import WorkerToolRequest
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.domain.worker_tools import WorkerToolService, open_executor, tool_signature
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import AgentDelegationRow, RunRow
from agent_workbench.tools.attachments import list_attachments, materialize_attachment
from agent_workbench.tools.commands import run_execution_kernel
from agent_workbench.tools.delegation import delegate_task
from agent_workbench.tools.runtime import ResourceLocks, Toolbox
from tests.integration.test_attachments import upload
from tests.integration.test_ultra_delegations import TOKEN, make_manager
from tests.unit.test_attachment_delivery import flask_zip

pytestmark = [pytest.mark.integration, pytest.mark.real_executor,
    pytest.mark.skipif(os.environ.get("REXAK_ATTACHMENT_KALI_ACCEPTANCE") != "1", reason="explicit Kali acceptance only")]


async def test_real_kali_main_and_worker_input(database, settings, monkeypatch):
    cfg = settings.model_copy(update={"ultra_worker_execution_enabled": True, "ultra_worker_token": TOKEN})
    manager = await make_manager(database, cfg)
    raw = flask_zip()
    with TestClient(create_app()) as api:
        headers = {"X-Workbench-CSRF": api.post("/api/session").json()["csrf_token"]}
        result = upload(api, manager.session_id, headers, raw, "flask.zip")
        assert result.status_code == 201, result.text
        aid = result.json()["attachment_id"]
        result = api.post(f"/api/sessions/{manager.session_id}/messages",
            json={"content": "synthetic file acceptance", "attachment_ids": [aid]},
            headers={**headers, "Idempotency-Key": str(uuid4())})
        assert result.status_code == 202
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        version = run.control_version
        # Test claim, in the disposable database only.
        run.status = "running"
    recipe = Path("tests/support/extract_attachment.py").read_text(encoding="utf-8")
    recipe = recipe.split('if __name__ == "__main__":')[0]
    records: dict = {"session_id": str(manager.session_id), "main": None, "workers": [],
        "real_kali": True, "paid_model": False, "online_target": False,
        "a2a_start": "deterministic test boundary; no model runtime"}
    async with open_executor(cfg) as remote:
        box = Toolbox(db=database, settings=cfg, session_id=manager.session_id, run_id=manager.run_id,
            owner="test-owner", owner_generation=1, control_version=version, executor=remote,
            scope=ScopePolicy(ScopeSnapshot(mode="instruction")), artifacts=ArtifactStore(cfg),
            spans=SpanRecorder(), locks=ResourceLocks(cfg))
        runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box), tool_call_id="kali-main-input")
        assert aid in await cast(Any, list_attachments).coroutine(runtime)
        main = json.loads(await cast(Any, materialize_attachment).coroutine(runtime, aid))
        assert main["status"] == "succeeded", main
        records["main"] = main
        path = main["input_file"]["relative_path"]
        code = recipe + f"\nprint(extract({path!r}, 'challenge_src'))\n"
        code += "\nfrom pathlib import Path\nassert 'is_admin' in Path('challenge_src/app/routes.py').read_text()\n"
        code += "assert Path('challenge_src/program.bin').read_bytes() == b'\\x7fELF\\x00\\xff'\n"
        code += f"import hashlib\nassert hashlib.sha256(Path({path!r}).read_bytes()).hexdigest() == {main['input_file']['sha256']!r}\n"
        command = f"unzip -l {shlex.quote(path)} && python3 -c {shlex.quote(code)} && find challenge_src -type f && grep -RIn is_admin challenge_src/app"
        execution = await run_execution_kernel(box, tool_call_id="kali-main-analysis", tool_name="run_command",
            capability="command", action="run_command", arguments={"workspace_id": str(manager.session_id),
                "command": command, "execution_timeout_seconds": 30}, wait_ms=10000)
        assert execution.data["exit_code"] == 0, execution.model_dump()
        records["main_analysis"] = {"operation_id": str(execution.operation_id), "exit_code": execution.data["exit_code"]}
        remote_records = {}

        async def submit(_self, ident, payload):
            # Delivery must be complete BEFORE even this deterministic A2A-start substitute.
            assert len(payload["input_files"]) == 1
            task, worker = str(uuid4()), uuid4()
            async with database.transaction() as db:
                row = await db.get(AgentDelegationRow, ident)
                row.state, row.a2a_task_id, row.worker_id = "working", task, str(worker)
                context = row.context_id
            remote_records[ident] = {"phase": "running", "task_id": task, "worker_id": str(worker),
                "task": {"contextId": str(context)}}
            return ident

        async def enter(self):
            return self

        async def lookup(_self, ident):
            return remote_records.get(ident)

        async def project(self, ident, _task):
            return await self.read(ident)

        monkeypatch.setattr(A2AWorkerClient, "__aenter__", enter)
        monkeypatch.setattr(A2AWorkerClient, "submit", submit)
        monkeypatch.setattr(A2AWorkerClient, "lookup", lookup)
        from agent_workbench.domain.agent_delegations import DelegationManager

        monkeypatch.setattr(DelegationManager, "_project", project)
        service = WorkerToolService(database, cfg, remote)
        for index in range(2):
            runtime.tool_call_id = f"kali-delegate-{index}"
            delegated = json.loads(await cast(Any, delegate_task).coroutine(runtime,
                objective="analyze synthetic Flask source", attachment_ids=[aid]))
            ident = UUID(delegated["id"])
            assert delegated["state"] == "working", delegated
            async with database.session() as db:
                row = await db.get(AgentDelegationRow, ident)
                info = row.handoff_snapshot["input_files"][0]
                task, worker, context = row.a2a_task_id, UUID(row.worker_id), row.context_id

            async def call(tool, phase, args=None, *, call_id="shell", step=0, operation=None,
                           task=task, worker=worker, context=context, ident=ident):
                value = WorkerToolRequest(task_id=task, worker_id=worker, context_id=context,
                    tool_call_id=call_id, tool=tool, phase=phase, step=step,
                    arguments=args or {}, operation_id=operation)
                return await service.handle(ident, value, tool_signature(TOKEN, ident, value))

            async def perform(tool, args, call_id, call=call):
                await call(tool, "begin", args, call_id=call_id)
                result = await call(tool, "step", args, call_id=call_id, step=1)
                for _ in range(100):
                    if not result.get("pending"):
                        break
                    await asyncio.sleep(0.1)
                    result = await call(tool, "poll", call_id=call_id, operation=UUID(result["operation_id"]))
                assert (result.get("data", {}).get("exit_code") == 0 if tool == "bash" else result.get("ok")), result
                await call(tool, "finish", {"action": "unused"}, call_id=call_id)
                return result

            code = recipe + f"\nprint(extract({info['relative_path']!r}, 'worker_src'))\n"
            cmd = f"unzip -l {shlex.quote(info['relative_path'])} && python3 -c {shlex.quote(code)} && find worker_src -type f && grep -RIn is_admin worker_src/app"
            shell = await perform("bash", {"action": "bash", "command": cmd}, "analyze")
            assert "is_admin" in shell["data"]["output"]
            read = await perform("read", {"action": "read", "path": "worker_src/app/security.py"}, "read-source")
            assert b"is_admin" in base64.b64decode(read["data"]["data_base64"])
            grep = await perform("grep", {"action": "grep", "path": "worker_src/app", "pattern": "is_admin"}, "search-source")
            assert grep["data"]["lines"]
            found = await perform("find", {"action": "glob", "path": "worker_src", "pattern": "*.py"}, "find-source")
            assert found["data"]["lines"]
            records["workers"].append({"delegation_id": str(ident), "input": info,
                "bash": True, "read": True, "grep": True, "find": True})
            async with database.transaction() as db:
                row = await db.get(AgentDelegationRow, ident)
                row.state = "completed"
        assert (await remote.health()).active_jobs == 0
    output = os.environ.get("REXAK_ATTACHMENT_EVIDENCE")
    if output:
        Path(output).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
