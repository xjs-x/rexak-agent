"""Harmless Stage 3 repair acceptance against a separate Linux Executor process/data/venv.

Only the dedicated fixture is started/stopped. Daily Executor, models and targets are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncssh
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workbench.config.deployment import remote_path
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.contracts.worker_tools import WorkerToolRequest
from agent_workbench.domain.run_state import create_run
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.domain.worker_tools import WorkerToolService, open_executor, tool_signature
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.execution.client import ExecutorConnectionError
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import SessionRepository
from agent_workbench.persistence.tables import AgentDelegationRow
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8
from agent_workbench.tools.commands import run_execution_kernel
from agent_workbench.tools.runtime import ResourceLocks, Toolbox


@asynccontextmanager
async def fixture_executor(cfg: Settings, wheel: Path):
    async with asyncssh.connect(
        cfg.executor_host,
        port=cfg.executor_port,
        username=cfg.executor_user,
        client_keys=[str(cfg.executor_ssh_key)],
        known_hosts=str(cfg.executor_known_hosts),
        connect_timeout=10,
    ) as conn:
        home = str((await conn.run('printf %s "$HOME"', check=True)).stdout).strip()
        production = remote_path(cfg.executor_install_root, home)
        root = home + "/rexak-review-fixture-" + uuid4().hex
        token = secrets.token_hex(32)
        await conn.run("mkdir -m 700 " + shlex.quote(root), check=True)
        async with conn.start_sftp_client() as sftp:
            await sftp.put(str(wheel), root + "/" + wheel.name)
        bootstrap = r"""import json,os,pathlib,shutil,socket,subprocess,sys
v=json.load(sys.stdin); root=pathlib.Path(v['root']); source=pathlib.Path(v['production'])
assert root.parent==pathlib.Path.home() and root.name.startswith('rexak-review-fixture-')
shutil.copytree(source/'.venv',root/'.venv',symlinks=True)
python=str(root/'.venv/bin/python')
r=subprocess.run([python,'-m','pip','install','--no-deps','--force-reinstall',str(root/v['wheel'])],capture_output=True)
if r.returncode: raise RuntimeError('fixture wheel installation failed')
with socket.socket() as s: s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
env={'PATH':str(root/'.venv/bin')+os.pathsep+os.defpath,'LANG':'C.UTF-8','PYTHONIOENCODING':'utf-8',
 'EXECUTOR_TOKEN':v['token'],'EXECUTOR_EXECUTOR_ID':root.name,'EXECUTOR_PORT':str(port),
 'EXECUTOR_DATA_ROOT':str(root/'data'),'EXECUTOR_WORKSPACE_ROOT':str(root/'data/workspaces'),
 'EXECUTOR_BROWSER_ENABLED':'false','EXECUTOR_EXECUTION_MODE':'trusted','EXECUTOR_RESOURCE_LOG_LIMIT_BYTES':'1048576'}
with (root/'fixture.log').open('ab') as log:
 p=subprocess.Popen([python,'-m','workbench_executor'],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
identity={'root':str(root),'pid':p.pid,'start_ticks':pathlib.Path(f'/proc/{p.pid}/stat').read_text().split(') ',1)[1].split()[19],'port':port}
(root/'fixture-identity.json').write_text(json.dumps(identity))
print(json.dumps(identity))
"""
        result = await conn.run(
            "python3 -c " + shlex.quote(bootstrap),
            input=json.dumps(
                {"root": root, "production": production, "wheel": wheel.name, "token": token}
            ),
            check=True,
            timeout=120,
        )
        identity = json.loads(str(result.stdout))
        isolated = cfg.model_copy(
            update={
                "executor_install_root": root,
                "executor_token": token,
                "executor_id": Path(root).name,
                "executor_remote_port": identity["port"],
                "executor_local_port": 0,
            }
        )
        try:
            yield isolated, identity
        finally:
            shutdown = r"""import json,os,pathlib,signal,time
root=pathlib.Path(__import__('sys').argv[1]); v=json.loads((root/'fixture-identity.json').read_text()); p=pathlib.Path(f"/proc/{v['pid']}")
if p.exists():
 assert (p/'stat').read_text().split(') ',1)[1].split()[19]==v['start_ticks']
 assert str(root).encode() in (p/'cmdline').read_bytes()
 os.kill(v['pid'],signal.SIGTERM)
 for _ in range(100):
  if not p.exists(): break
  if (p/'stat').read_text().split(') ',1)[1].split()[0]=='Z': break
  time.sleep(.1)
 else: raise RuntimeError('fixture did not stop')
print('Dedicated fixture stopped; files retained')
"""
            await conn.run(
                "python3 -c " + shlex.quote(shutdown) + " " + shlex.quote(root),
                check=True,
                timeout=20,
            )


async def smoke(cfg: Settings, output: Path):
    db = Database(cfg)
    await db.start()
    ids = [(uuid4(), uuid4(), uuid4(), str(uuid4())) for _ in range(2)]
    try:
        async with db.transaction() as session:
            parent = await SessionRepository(session).create(
                project_id=None,
                title="Stage 3 review synthetic",
                execution_mode="ultra",
                scope_snapshot={},
                policy_snapshot={},
            )
            run = await create_run(
                session,
                session_id=parent.id,
                objective="Synthetic isolated acceptance",
                provider_id="mock",
                model_name="no-model",
                config_snapshot={},
            )
            run.status, run.owner, run.owner_generation = "running", "review-fixture", 1
            run.lease_until = datetime.now(UTC) + timedelta(minutes=10)
            for did, _wid, cid, _task in ids:
                session.add(
                    AgentDelegationRow(
                        id=did,
                        parent_session_id=parent.id,
                        parent_run_id=run.id,
                        parent_tool_call_id=str(did),
                        request_digest="0" * 64,
                        context_id=cid,
                        objective="synthetic",
                        context_refs=[],
                        handoff_snapshot={
                            "execution_access": "pi_tools_v1",
                            "evidence_access": "session_read_v1",
                        },
                        authorization_snapshot={
                            "executor_access": True,
                            "owner_generation": 1,
                            "control_version": 0,
                            "tool_allowlist": ["read", "write", "edit", "grep", "find", "bash"],
                        },
                        state="working",
                        summary="",
                        cancel_requested=False,
                    )
                )

        async def lookup(_client, ident):
            for did, wid, cid, task in ids:
                if ident == did:
                    return {
                        "phase": "running",
                        "task_id": task,
                        "worker_id": str(wid),
                        "task": {"contextId": str(cid)},
                    }
            return None

        async with open_executor(cfg) as executor:
            for _ in range(50):
                try:
                    await executor.health()
                    break
                except ExecutorConnectionError:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("fixture startup failed")
            service = WorkerToolService(db, cfg, executor)

            async def call(
                index, tool, phase, arguments=None, *, call_id="call", step=0, operation_id=None
            ):
                did, wid, cid, task = ids[index]
                value = WorkerToolRequest(
                    tool=tool,
                    phase=phase,
                    arguments=arguments or {},
                    task_id=task,
                    worker_id=wid,
                    context_id=cid,
                    tool_call_id=call_id,
                    step=step,
                    operation_id=operation_id,
                )
                return await service.handle(
                    did, value, tool_signature(cfg.ultra_worker_token, did, value)
                )

            async def complete(tool, args, key, expected_error=None):
                await call(1, tool, "begin", args, call_id=key)
                result = await call(1, tool, "step", args, call_id=key, step=1)
                for _ in range(120):
                    if not result.get("pending"):
                        break
                    await asyncio.sleep(0.1)
                    result = await call(
                        1, tool, "poll", call_id=key, operation_id=result["operation_id"]
                    )
                assert not result.get("uncertain"), result
                assert result.get("error") == expected_error, result
                return result

            box = Toolbox(
                run_id=run.id,
                session_id=parent.id,
                owner=run.owner,
                owner_generation=1,
                settings=cfg,
                db=db,
                scope=ScopePolicy(ScopeSnapshot()),
                executor=executor,
                artifacts=ArtifactStore(cfg),
                spans=SpanRecorder(),
                locks=ResourceLocks(cfg),
            )

            async def main_command(key, command, wait=0):
                return await run_execution_kernel(
                    box,
                    tool_call_id=key,
                    tool_name="run_command",
                    capability="command",
                    action="run_command",
                    arguments={"command": command, "workspace_id": str(parent.id)},
                    wait_ms=wait,
                )

            with patch.object(A2AWorkerClient, "lookup", lookup):
                await asyncio.gather(
                    call(0, "bash", "begin", {"command": "sleep 4; touch cancelled.marker"}),
                    call(1, "bash", "begin", {"command": "sleep 3; touch sibling.marker"}),
                )
                one, two, main = await asyncio.gather(
                    call(
                        0,
                        "bash",
                        "step",
                        {"action": "bash", "command": "sleep 4; touch cancelled.marker"},
                        step=1,
                    ),
                    call(
                        1,
                        "bash",
                        "step",
                        {"action": "bash", "command": "sleep 3; touch sibling.marker"},
                        step=1,
                    ),
                    main_command("main-parallel", "sleep 3; touch main.marker"),
                )
                assert one.get("pending") and two.get("pending")
                stopped = await call(0, "bash", "settle", {"cancel": True})
                assert stopped["settled"], stopped
                first_state = await executor.execution_resource(one["resource_id"])
                assert (
                    first_state["state"] in {"exited", "closed"}
                    and first_state["termination_reason"] == "user_cancelled"
                )
                await asyncio.sleep(3.5)
                two = await call(1, "bash", "poll", operation_id=two["operation_id"])
                main_state = await executor.execution_resource(str(main.job_id))
                assert two["data"]["exit_code"] == 0 and main_state["exit_code"] == 0
                check = await main_command(
                    "check-markers",
                    f"test ! -e .rexak/workers/{ids[0][0]}/cancelled.marker && test -e .rexak/workers/{ids[1][0]}/sibling.marker && test -e main.marker",
                    10000,
                )
                assert check.status == "succeeded", check.summary
                # These synthetic modules would execute if Python imported from Worker cwd.
                await complete(
                    "bash",
                    {
                        "action": "bash",
                        "command": 'mkdir -p workbench_executor; printf \'open("IMPORT_POLLUTION","w").write("bad")\' > json.py; cp json.py workbench_executor/__init__.py; printf \'before\\nneedle\\nafter\\n\' > note.txt',
                    },
                    "seed-import",
                )
                read = await complete(
                    "read", {"action": "read", "path": "note.txt"}, "read-isolated"
                )
                assert read["ok"]
                grep = await complete(
                    "grep",
                    {"action": "grep", "path": ".", "pattern": "needle", "context": 2, "limit": 1},
                    "grep-isolated",
                )
                assert any(":2:needle" in line for line in grep["data"]["lines"])
                found = await complete(
                    "find",
                    {"action": "glob", "path": ".", "pattern": "*.txt", "limit": 100},
                    "find-isolated",
                )
                assert "note.txt" in found["data"]["lines"]
                safe = await complete(
                    "bash",
                    {"action": "bash", "command": "test ! -e IMPORT_POLLUTION"},
                    "check-import",
                )
                assert safe["data"]["exit_code"] == 0
                output_cases = {}
                for name, statement in [
                    ("exact-64k", "sys.stdout.buffer.write(b'a'*65536)"),
                    ("over-64k", "sys.stdout.buffer.write(b'a'*65533+b'TAIL')"),
                    (
                        "utf8-tail",
                        "sys.stdout.buffer.write(bytes.fromhex('e4bda0')*30000+b'TAIL!')",
                    ),
                    (
                        "binary-stderr",
                        "sys.stdout.write('known');sys.stderr.buffer.write(bytes([255,254]))",
                    ),
                    ("collection-gap", "sys.stdout.buffer.write(b'x'*1100000)"),
                ]:
                    result = await complete(
                        "bash",
                        {
                            "action": "bash",
                            "command": "python3 -I -c " + shlex.quote("import sys;" + statement),
                        },
                        name,
                    )
                    assert result["data"]["exit_code"] == 0
                    if name == "exact-64k":
                        assert not result["data"]["truncated"]
                    if name in {"over-64k", "utf8-tail"}:
                        assert "TAIL" in result["data"]["output"] and result["data"]["truncated"]
                    if name == "binary-stderr":
                        assert result["output_refs"][1]["decode_status"] == "invalid_utf8"
                    if name == "collection-gap":
                        assert (
                            result["output_refs"][0]["missing_ranges"]
                            and result["data"]["truncated"]
                        )
                    output_cases[name] = result["output_refs"]
                conflict = await complete(
                    "edit",
                    {
                        "action": "write",
                        "path": "note.txt",
                        "content": "BAD",
                        "expected_version": "0" * 64,
                    },
                    "edit-conflict",
                    expected_error="version_conflict",
                )
                assert not conflict["ok"] and conflict["error"] == "version_conflict"
                assert (await call(1, "bash", "settle", {"cancel": False}))["settled"]
            output.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "captured_at": datetime.now(UTC).isoformat(),
                        "executor_id": cfg.executor_id,
                        "parallel_cancel_confirmed": True,
                        "sibling_and_main_completed": True,
                        "cwd_import_not_executed": True,
                        "grep_find_passed": True,
                        "edit_conflict_failed": True,
                        "output_cases": output_cases,
                        "real_model_calls": 0,
                        "target_traffic": False,
                        "a2a_identity_lookup": "synthetic-bound-test; actual cross-language A2A is in the integration suite",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    finally:
        await db.close()


async def readback_fixture(cfg: Settings, receipt: Path, output: Path):
    """Verify retained immutable synthetic executions, without running the commands again."""
    identity = json.loads(receipt.read_text(encoding="utf-8"))
    code = r"""import base64,hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]); assert root.parent==pathlib.Path.home() and root.name.startswith('rexak-review-fixture-')
records=[]; cases={}; file_actions={}; cancelled=[]
for path in (root/'data/jobs').glob('*/spec.json'):
 spec=json.loads(path.read_text()); finish=json.loads((path.parent/'completion.json').read_text()); raw=(path.parent/'stdout.log').read_bytes()
 index=json.loads((path.parent/'output-index.json').read_text()); stdout=index['streams']['stdout']; stderr=(path.parent/'stderr.log').read_bytes()
 command=spec['argv'][-1]; rec={'operation_id':spec['operation_id'],'resource_id':spec['job_id'],'exit_code':finish['exit_code'],'termination_reason':finish['termination_reason'],'stored_stdout_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
 records.append(rec)
 if finish['termination_reason']=='user_cancelled': cancelled.append(rec)
 if spec.get('stdin_text') and 'worker_files' in ' '.join(spec['argv']):
  assert '-I' in spec['argv']; request=json.loads(spec['stdin_text']); result=json.loads(raw); file_actions[request['action']]=rec
  if request['action']=='read': assert base64.b64decode(result['data']['data_base64'])==b'before\nneedle\nafter\n'
  if request['action']=='grep': assert any(':2:needle' in line for line in result['data']['lines'])
  if request['action']=='glob': assert 'note.txt' in result['data']['lines']
  if request['action']=='write': assert result=={'ok':False,'error':'version_conflict'} and finish['exit_code']==1
 for name,marker in [('exact-64k','65536'),('over-64k','65533'),('utf8-tail','fromhex'),('binary-stderr','255,254'),('collection-gap','1100000')]:
  if marker in command:
   assert finish['exit_code']==0
   if name=='exact-64k': assert len(raw)==65536 and stdout['source_bytes']==65536 and not stdout['drops']
   if name in {'over-64k','utf8-tail'}: assert b'TAIL' in raw[-65536:] and len(raw)>65536
   if name=='binary-stderr': assert stderr==bytes([255,254])
   if name=='collection-gap': assert stdout['drops'] and stdout['source_bytes']>len(raw)
   cases[name]={**rec,'source_stdout_bytes':stdout['source_bytes'],'drops':stdout['drops']}
assert len(cancelled)==1 and {'read','grep','glob','write'}<=set(file_actions) and len(cases)==5
workspaces=list((root/'data/workspaces').iterdir()); assert len(workspaces)==1; workspace=workspaces[0]
assert (workspace/'main.marker').is_file() and len(list(workspace.rglob('sibling.marker')))==1
assert not list(workspace.rglob('cancelled.marker')) and not list(workspace.rglob('IMPORT_POLLUTION'))
assert next(workspace.rglob('note.txt')).read_bytes()==b'before\nneedle\nafter\n'
proc=pathlib.Path('/proc')/str(json.loads((root/'fixture-identity.json').read_text())['pid'])
assert not proc.exists() or (proc/'stat').read_text().split(') ',1)[1].split()[0]=='Z'
print(json.dumps({'passed':True,'verification':'retained-execution-readback; no commands re-executed','job_count':len(records),'parallel_cancel_confirmed':True,'sibling_and_main_completed':True,'cwd_import_not_executed':True,'grep_find_passed':True,'edit_conflict_failed':True,'fixture_stopped':True,'output_cases':cases,'file_actions':file_actions,'real_model_calls':0,'target_traffic':False}))
"""
    async with asyncssh.connect(
        cfg.executor_host,
        port=cfg.executor_port,
        username=cfg.executor_user,
        client_keys=[str(cfg.executor_ssh_key)],
        known_hosts=str(cfg.executor_known_hosts),
        connect_timeout=10,
    ) as conn:
        result = await conn.run(
            "python3 -c " + shlex.quote(code) + " " + shlex.quote(identity["root"]),
            check=True,
            timeout=15,
        )
    data = json.loads(str(result.stdout))
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Retained Linux executions verified; fixture stopped; no commands repeated")


def main():
    configure_stdio_utf8()
    configure_event_loop_policy()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--readback-fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = get_settings()
    if args.readback_fixture:
        asyncio.run(readback_fixture(original, args.readback_fixture, args.output))
        return
    if not args.wheel:
        parser.error("--wheel is required for execution acceptance")
    url = make_url(original.database_url)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    name = "workbench_review_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with tempfile.TemporaryDirectory(prefix="rexak-review-") as root:
            isolated_url = url.set(database=name).render_as_string(hide_password=False)
            env = {
                **os.environ,
                "WORKBENCH_DATABASE_URL": isolated_url,
                "WORKBENCH_DATA_ROOT": root,
                "PYTHONIOENCODING": "utf-8",
            }
            subprocess.run(
                [sys.executable, "-m", "agent_workbench.cli", "db-init"],
                env=env,
                check=True,
                capture_output=True,
                timeout=90,
            )
            cfg = original.model_copy(
                update={
                    "database_url": isolated_url,
                    "data_root": Path(root),
                    "ultra_worker_token": secrets.token_hex(32),
                }
            )

            async def verify():
                async with fixture_executor(cfg, args.wheel) as (isolated, identity):
                    args.output.with_suffix(".fixture.json").write_text(
                        json.dumps(identity, indent=2) + "\n", encoding="utf-8"
                    )
                    await smoke(isolated, args.output)

            asyncio.run(verify())
            print("Separate Linux Executor repair acceptance passed; no models or targets")
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
