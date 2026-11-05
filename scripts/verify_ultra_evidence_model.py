"""Explicit live Pi evidence verification: isolated Session/DB, synthetic history, no target access."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.config.worker_model import read_worker_model
from agent_workbench.contracts.delegation import DelegationInput, WorkerResult
from agent_workbench.domain.agent_delegations import DelegationManager
from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ArtifactRepository, SessionRepository
from agent_workbench.persistence.tables import ArtifactRow, DelegationEvidenceReadRow
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8


def free_port() -> int:
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)


async def verify(output: Path) -> bool:
    base = get_settings()
    model = read_worker_model(base)
    if not model or not shutil.which("node"):
        raise RuntimeError("需要独立 Worker 模型配置和 Node")
    name = "workbench_evidence_verify_" + uuid4().hex
    url = make_url(base.database_url)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    isolated = url.set(database=name).render_as_string(hide_password=False)
    report: dict = {"started_at": datetime.now(UTC).isoformat(), "provider": model.provider, "model": model.model,
        "temperature": model.temperature, "max_tokens": model.max_tokens, "timeout_ms": model.timeout_ms,
        "real_model": True, "target_access": False, "execution_tools": "mock_only", "passed": False}
    created = False
    try:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        with tempfile.TemporaryDirectory(prefix="rexak-evidence-verify-") as root, ExitStack() as stack:
            api_port, worker_port = free_port(), free_port()
            token = secrets.token_urlsafe(32)
            env = {**os.environ, "WORKBENCH_DATABASE_URL": isolated, "WORKBENCH_DATA_ROOT": root,
                "WORKBENCH_OWNER_ID": str(base.owner_id), "WORKBENCH_ULTRA_WORKER_TOKEN": token,
                "WORKBENCH_ULTRA_WORKER_URL": f"http://127.0.0.1:{worker_port}", "PYTHONIOENCODING": "utf-8"}
            migration = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "agent_workbench.cli", "db-init"],
                cwd=REPO_ROOT, env=env, capture_output=True, timeout=90, check=False)
            if migration.returncode:
                raise RuntimeError("隔离库迁移失败")
            api_log = stack.enter_context((Path(root) / "api.log").open("wb"))
            worker_log = stack.enter_context((Path(root) / "worker.log").open("wb"))
            api = subprocess.Popen([sys.executable, "-m", "agent_workbench.cli", "api", "--port", str(api_port)],
                cwd=REPO_ROOT, env=env, stdout=api_log, stderr=api_log)
            stack.callback(stop, api)
            child = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
            child.update(REXAK_WORKER_DATABASE_URL=make_url(isolated).set(drivername="postgresql").render_as_string(hide_password=False),
                REXAK_WORKER_TOKEN=token, REXAK_WORKER_PORT=str(worker_port), REXAK_WORKER_CONTROL_URL=f"http://127.0.0.1:{api_port}",
                REXAK_WORKER_MODEL_CONFIG=json.dumps(model.transport_value()))
            worker = subprocess.Popen([shutil.which("node") or "node", str(REPO_ROOT / "worker/dist/server.js")],
                cwd=REPO_ROOT, env=child, stdout=worker_log, stderr=worker_log)
            stack.callback(stop, worker)
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                for endpoint in [f"http://127.0.0.1:{api_port}/health", f"http://127.0.0.1:{worker_port}/health"]:
                    for _ in range(200):
                        if api.poll() is not None or worker.poll() is not None:
                            raise RuntimeError("验收服务启动失败")
                        try:
                            response = await client.get(endpoint, headers={"Authorization": "Bearer " + token})
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("验收服务就绪超时")
            cfg = base.model_copy(update={"database_url": isolated, "data_root": Path(root),
                "ultra_worker_url": f"http://127.0.0.1:{worker_port}", "ultra_worker_token": token, "ultra_worker_execution_enabled": False})
            db = Database(cfg)
            await db.start()
            try:
                left, right = str(secrets.randbelow(90) + 10), str(secrets.randbelow(90) + 10)
                second_id, first_id = uuid4(), uuid4()
                async with db.transaction() as session:
                    parent = await SessionRepository(session).create(project_id=None, title="Isolated evidence model check",
                        scope_snapshot={}, policy_snapshot={}, execution_mode="ultra")
                    run = await create_run(session, session_id=parent.id, objective="Historical evidence analysis",
                        provider_id="mock", model_name="mock", config_snapshot={})
                    run.status, run.owner, run.owner_generation = "running", "evidence-verify", 1
                    run.lease_until = datetime.now(UTC) + timedelta(minutes=20)
                    sid, rid = parent.id, run.id
                    for ident, summary, body in [(first_id, "chain-start", f"PART={left}\nNEXT={second_id}\n"),
                            (second_id, "chain-second", f"PART={right}\nThis is synthetic history, not a real credential.\n")]:
                        stored = ArtifactStore(cfg).write_bytes(body.encode(), media_type="text/plain")
                        await ArtifactRepository(session).create(artifact_id=ident, sha256=stored.sha256, byte_size=stored.byte_size,
                            media_type=stored.media_type, relative_key=stored.relative_key, session_id=sid,
                            source_operation_id=None, sensitivity="internal", availability="available", captured_at=stored.captured_at, summary=summary)
                manager = DelegationManager(db, cfg, sid, rid)
                record = await manager.delegate("live-evidence-once", DelegationInput(
                    objective="从本 Session 的历史证据推导两段拼接代码：从 chain-start 开始，按证据中的下一份材料线索继续；最终给出拼接代码和依据。",
                    background="仅离线合成验收，没有真实目标。初始引用只是推荐材料；需要真实调用证据工具取得内容，不能猜测代码。",
                    context_refs=[first_id], completion_requirements=["读取两份实际历史材料", "最终包含两段 PART 按线索顺序拼接的代码", "不得声称漏洞已确认"]),
                    owner="evidence-verify", generation=1, control_version=0)
                deadline = asyncio.get_running_loop().time() + 600
                while record.state not in {"completed", "failed", "cancelled", "rejected"}:
                    if asyncio.get_running_loop().time() >= deadline:
                        await manager.cancel(record.id)
                        raise RuntimeError("验收等待超时，已提交取消意图")
                    record = await manager.wait(record.id, 20)
                report.update(status=str(record.state), diagnostic=record.diagnostic, delegation_id=str(record.id),
                    task_id=record.a2a_task_id, context_id=str(record.context_id), expected_synthetic_code=left + right)
                async with db.session() as session:
                    calls = (await session.execute(text("SELECT sequence,metric FROM ultra_worker.model_calls WHERE request_id=:id ORDER BY sequence"), {"id": record.id})).all()
                    report["model_calls"] = [{"sequence": call.sequence, "metric": call.metric} for call in calls]
                    receipts = list((await session.scalars(select(DelegationEvidenceReadRow).where(DelegationEvidenceReadRow.delegation_id == record.id))).all())
                    report["read_audit_count"] = len(receipts)
                    if record.result_artifact_id:
                        artifact = await session.get(ArtifactRow, record.result_artifact_id)
                        assert artifact
                        result = WorkerResult.model_validate_json(ArtifactStore(cfg).read_bytes(artifact.relative_key))
                        report.update(summary=result.summary, evidence_refs=result.evidence_refs,
                            evidence_reads=[r.model_dump(mode="json") for r in result.evidence_reads],
                            model_usage=result.model_usage.model_dump() if result.model_usage else None)
                        report["passed"] = (record.state == "completed" and left + right in result.summary
                            and set(result.evidence_refs) == {str(first_id), str(second_id)} and len(calls) >= 3)
            finally:
                await db.close()
    except (RuntimeError, OSError, ValueError, psycopg.Error, httpx.HTTPError) as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if created:
            with psycopg.connect(admin, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        report["finished_at"] = datetime.now(UTC).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: report.get(k) for k in ("status", "passed", "model_usage", "read_audit_count", "error_type")}, ensure_ascii=False))
    return report["passed"] is True


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_event_loop_policy()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
