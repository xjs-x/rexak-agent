"""Explicit harmless Kali smoke: authenticated controller service/kernel, no model or target traffic."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.worker_tools import WorkerToolRequest
from agent_workbench.domain.run_state import create_run
from agent_workbench.domain.worker_tools import WorkerToolService, open_executor, tool_signature
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import SessionRepository
from agent_workbench.persistence.tables import AgentDelegationRow, ToolOperationRow
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8


async def smoke(cfg, output: Path, environment_only: bool = False):
    database = Database(cfg)
    await database.start()
    did, wid, cid, task = uuid4(), uuid4(), uuid4(), str(uuid4())
    try:
        async with database.transaction() as db:
            parent = await SessionRepository(db).create(project_id=None, title="Stage 3 isolated harmless smoke",
                execution_mode="ultra", scope_snapshot={}, policy_snapshot={})
            run = await create_run(db, session_id=parent.id, objective="Harmless isolated file workflow",
                provider_id="mock", model_name="no-model", config_snapshot={})
            run.status, run.owner, run.owner_generation = "running", "stage3-smoke", 1
            run.lease_until = datetime.now(UTC) + timedelta(minutes=5)
            db.add(AgentDelegationRow(id=did, parent_session_id=parent.id, parent_run_id=run.id,
                parent_tool_call_id="smoke", request_digest="0" * 64, context_id=cid,
                handoff_snapshot={"execution_access": "pi_tools_v1", "evidence_access": "session_read_v1"}, objective="Harmless file smoke",
                context_refs=[], authorization_snapshot={"executor_access": True, "owner_generation": 1,
                    "control_version": 0, "tool_allowlist": ["read", "write", "edit", "grep", "find", "bash"]},
                state="working", summary="", cancel_requested=False))

        async def lookup(_client, ident):
            return {"phase": "running", "task_id": task, "worker_id": str(wid), "task": {"contextId": str(cid)}} if ident == did else None

        async with open_executor(cfg) as executor:
            service = WorkerToolService(database, cfg, executor)

            async def call(tool, phase, arguments=None, **extra):
                request = WorkerToolRequest(tool=tool, phase=phase, arguments=arguments or {}, tool_call_id=tool,
                    task_id=task, worker_id=wid, context_id=cid, **extra)
                result = await service.handle(did, request, tool_signature(cfg.ultra_worker_token, did, request))
                while result.get("pending"):
                    await asyncio.sleep(0.15)
                    request = request.model_copy(update={"phase": "poll", "arguments": {}, "operation_id": result["operation_id"]})
                    result = await service.handle(did, request, tool_signature(cfg.ultra_worker_token, did, request))
                assert not result.get("uncertain") and not result.get("error"), result
                return result

            outcomes = {}
            with patch.object(A2AWorkerClient, "lookup", lookup):
                sequence = [
                    ("write", {"action": "write", "path": "probe.txt", "content": "Alpha\n"}),
                    ("read", {"action": "read", "path": "probe.txt"}),
                    ("edit", {"action": "read", "path": "probe.txt"}),
                    ("grep", {"action": "grep", "path": ".", "pattern": "Beta"}),
                    ("find", {"action": "glob", "path": ".", "pattern": "*.txt", "limit": 10}),
                    ("bash", {"action": "bash", "command": "printf 'stage3-kali-ok\\n'", "timeout": 5}),
                ]
                if environment_only:
                    sequence = [("bash", {"action": "bash", "command": "printf 'stage3-kali-ok\\n%s\\n%s\\n' \"$HOME\" \"$TMPDIR\"", "timeout": 5})]
                for tool, arguments in sequence:
                    begun = await call(tool, "begin", arguments)
                    result = await call(tool, "step", arguments, step=1)
                    if tool in {"read", "edit"}:
                        assert base64.b64decode(result["data"]["data_base64"]) == b"Alpha\n"
                    if tool == "edit":
                        result = await call(tool, "step", {"action": "write", "path": "probe.txt", "content": "Beta\n",
                            "expected_version": result["data"]["version"]}, step=2)
                    if tool == "grep":
                        assert any("Beta" in line for line in result["data"]["lines"])
                    if tool == "find":
                        assert "probe.txt" in result["data"]["lines"]
                    if tool == "bash":
                        assert result["data"]["exit_code"] == 0 and "stage3-kali-ok" in result["data"]["output"]
                        if environment_only:
                            assert begun["workspace"] + "/.home" in result["data"]["output"]
                            assert begun["workspace"] + "/.tmp" in result["data"]["output"]
                    outcomes[tool] = {"operation_id": result["operation_id"], "artifact_id": result["artifact_id"]}
                    await call(tool, "finish", result={"content": [{"type": "text", "text": "Harmless smoke passed"}]})
                assert (await call("bash", "settle", {"cancel": False}))["settled"]
            async with database.session() as db:
                operations = list((await db.scalars(select(ToolOperationRow).where(ToolOperationRow.run_id == run.id))).all())
                assert all(op.status == "succeeded" for op in operations)
            output.write_text(json.dumps({"captured_at": datetime.now(UTC).isoformat(), "passed": True,
                "session_id": str(parent.id), "delegation_id": str(did), "operation_count": len(operations), "tools": outcomes,
                "real_model_calls": 0, "target_traffic": False, "environment_only": environment_only, "a2a_identity_lookup": "synthetic-bound-smoke-only",
                "boundary": "Real controller/Operation/fence/Executor; official Pi factories and A2A verified separately in isolated integration."},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        await database.close()


def main():
    configure_stdio_utf8()
    configure_event_loop_policy()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-executor", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-only", action="store_true")
    args = parser.parse_args()
    original = get_settings()
    url = make_url(original.database_url)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    name = "workbench_stage3_smoke_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with tempfile.TemporaryDirectory(prefix="rexak-stage3-") as root:
            isolated_url = url.set(database=name).render_as_string(hide_password=False)
            env = {**os.environ, "WORKBENCH_DATABASE_URL": isolated_url, "WORKBENCH_DATA_ROOT": root, "PYTHONIOENCODING": "utf-8"}
            result = subprocess.run([sys.executable, "-m", "agent_workbench.cli", "db-init"], env=env, capture_output=True, timeout=90)
            assert result.returncode == 0, "Isolated smoke database initialization failed"
            cfg = original.model_copy(update={"database_url": isolated_url, "data_root": Path(root), "ultra_worker_token": "stage3-isolated-smoke-token"})
            asyncio.run(smoke(cfg, args.output, args.environment_only))
            print("Private Kali environment passed" if args.environment_only else "Six harmless Kali operations passed; no model or target traffic")
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
