"""Full Pi → controller → durable Operation → Fake Executor, with two simultaneous Workers."""

import asyncio
import os
import shutil
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.delegation import DelegationInput, WorkerResult
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.tables import ArtifactRow, ToolOperationRow
from tests.integration.test_ultra_delegations import TOKEN, make_manager
from tests.integration.test_worker_evidence import artifact, free_port

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("bash_only", [False, True], ids=["eight-tools", "bash-output-without-evidence-read"])
async def test_all_eight_tools_over_a2a_with_parallel_workers(database, settings, tmp_path, bash_only):
    api_port, worker_port = free_port(), free_port()
    cfg = settings.model_copy(update={"ultra_worker_url": f"http://127.0.0.1:{worker_port}",
        "ultra_worker_token": TOKEN, "ultra_worker_execution_enabled": True})
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}}
    env.update(WORKBENCH_DATABASE_URL=settings.database_url, WORKBENCH_DATA_ROOT=str(settings.data_root),
        WORKBENCH_ULTRA_WORKER_URL=cfg.ultra_worker_url, WORKBENCH_ULTRA_WORKER_TOKEN=TOKEN,
        WORKBENCH_ULTRA_WORKER_EXECUTION_ENABLED="true", WORKBENCH_OWNER_ID=str(settings.owner_id),
        WORKBENCH_API_PORT=str(api_port), PI_FAKE_ROOT=str(tmp_path / "kali"),
        REXAK_WORKER_DATABASE_URL=make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False),
        REXAK_WORKER_TOKEN=TOKEN, FIXTURE_PORT=str(worker_port), REXAK_WORKER_CONTROL_URL=f"http://127.0.0.1:{api_port}", PYTHONIOENCODING="utf-8")
    env["FIXTURE_BASH_ONLY"] = "true" if bash_only else "false"
    node = shutil.which("node")
    assert node
    with (tmp_path / "api.log").open("wb") as api_log, (tmp_path / "worker.log").open("wb") as worker_log:
        api = subprocess.Popen([sys.executable, "-m", "tests.support.pi_api"], env=env, cwd=REPO_ROOT, stdout=api_log, stderr=api_log)
        worker = subprocess.Popen([node, "--import", "tsx", "test/tools-a2a-server.ts"], env=env, cwd=REPO_ROOT / "worker", stdout=worker_log, stderr=worker_log)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                for url in [f"http://127.0.0.1:{api_port}/health", cfg.ultra_worker_url + "/health"]:
                    for _ in range(200):
                        assert api.poll() is None and worker.poll() is None
                        try:
                            response = await client.get(url, headers={"Authorization": "Bearer " + TOKEN})
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        raise AssertionError("Pi tools runtime startup timeout")
            manager = await make_manager(database, cfg)
            history = await artifact(database, cfg, manager.session_id, b"prior synthetic progress", "stage3-history")
            records = await asyncio.gather(*(manager.delegate(f"parallel-{index}",
                DelegationInput(objective="Use six Kali tools and inspect saved evidence", context_refs=[history]),
                owner="test-owner", generation=1, control_version=0) for index in range(2)))
            results = await asyncio.gather(*(manager.wait(record.id, 45) for record in records))
            for record, result in zip(records, results, strict=True):
                assert result.state == "completed", result.model_dump_json()
                assert "worker-ok" in result.summary
                if not bash_only:
                    assert "Beta" in result.summary
                    path = tmp_path / "kali" / str(manager.session_id) / str(record.id) / "note.txt"
                    assert path.read_text(encoding="utf-8") == "Beta"
                async with database.session() as db:
                    saved = await db.get(ArtifactRow, result.result_artifact_id)
                    output = WorkerResult.model_validate_json(ArtifactStore(cfg).read_bytes(saved.relative_key))
                    if bash_only:
                        assert not output.evidence_refs and not output.evidence_reads
                    else:
                        assert str(history) in output.evidence_refs and output.evidence_reads
                    assert output.output_refs and any(ref.stream == "stdout" for ref in output.output_refs)
                    assert all(str(ref.artifact_id) not in output.evidence_refs for ref in output.output_refs)
                    assert output.model_usage and output.model_usage.calls == (2 if bash_only else 10)
                    operations = list((await db.scalars(select(ToolOperationRow).where(
                        ToolOperationRow.run_id == manager.run_id,
                        ToolOperationRow.execution_context["agent_delegation_id"].astext == str(record.id)))).all())
                    assert {op.tool_name for op in operations if op.execution_context.get("worker_tool_aggregate")} == (
                        {"pi.bash"} if bash_only else {"pi.read", "pi.write", "pi.edit", "pi.grep", "pi.find", "pi.bash"})
                    assert all(op.status == "succeeded" for op in operations)
                    assert all(op.session_id == manager.session_id for op in operations)
                    assert str(record.id) != output.task_id
        finally:
            for process in (worker, api):
                process.terminate()
                process.wait(timeout=10)
