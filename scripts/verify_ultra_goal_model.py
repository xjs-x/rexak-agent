"""Explicitly authorized live Worker model check, isolated DB and mock tools only."""

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.config.worker_model import read_worker_model
from agent_workbench.contracts.delegation import DelegationInput, WorkerResult
from agent_workbench.domain.agent_delegations import DelegationManager
from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import SessionRepository
from agent_workbench.persistence.tables import ArtifactRow
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8


async def verify(output: Path) -> bool:
    base = get_settings()
    model = read_worker_model(base)
    if model is None:
        raise RuntimeError("先配置独立 Worker 模型")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node 不可用")
    name = "workbench_goal_verify_" + uuid4().hex
    url = make_url(base.database_url)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    isolated = url.set(database=name).render_as_string(hide_password=False)
    report: dict[str, object] = {"provider": model.provider, "model": model.model,
        "temperature": model.temperature, "max_tokens": model.max_tokens, "timeout_ms": model.timeout_ms,
        "real_model": True, "tools": "mock_only", "target_access": False,
        "started_at": datetime.now(UTC).isoformat(), "status": "not_completed"}
    created = False
    process = None
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        with tempfile.TemporaryDirectory(prefix="rexak-goal-verify-") as root:
            env = {**os.environ, "WORKBENCH_DATABASE_URL": isolated, "WORKBENCH_DATA_ROOT": root,
                   "PYTHONIOENCODING": "utf-8"}
            migration = await asyncio.to_thread(subprocess.run,
                [sys.executable, "-m", "agent_workbench.cli", "db-init"], cwd=REPO_ROOT,
                env=env, capture_output=True, timeout=90, check=False)
            if migration.returncode:
                raise RuntimeError("隔离数据库迁移失败")
            with socket.socket() as bound:
                bound.bind(("127.0.0.1", 0))
                port = bound.getsockname()[1]
            token = secrets.token_urlsafe(32)
            cfg = base.model_copy(update={"database_url": isolated, "data_root": Path(root),
                "ultra_worker_url": f"http://127.0.0.1:{port}", "ultra_worker_token": token, "ultra_worker_execution_enabled": False})
            child_env = {k: v for k, v in os.environ.items()
                         if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
            child_env.update(REXAK_WORKER_DATABASE_URL=make_url(isolated).set(drivername="postgresql").render_as_string(hide_password=False),
                             REXAK_WORKER_TOKEN=token, REXAK_WORKER_PORT=str(port),
                             REXAK_WORKER_MODEL_CONFIG=json.dumps(model.transport_value()))
            with (Path(root) / "worker.log").open("wb") as log:
                process = subprocess.Popen([node, str(REPO_ROOT / "worker/dist/server.js")],
                    cwd=REPO_ROOT, env=child_env, stdout=log, stderr=log)
                async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("Worker 启动失败")
                        try:
                            health = await client.get(cfg.ultra_worker_url + "/health", headers={"Authorization": "Bearer " + token})
                            if health.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("Worker 就绪超时")
                db = Database(cfg)
                await db.start()
                try:
                    async with db.transaction() as session:
                        parent = await SessionRepository(session).create(project_id=None, title="Isolated live Pi verification",
                            scope_snapshot={}, policy_snapshot={}, execution_mode="ultra")
                        run = await create_run(session, session_id=parent.id, objective="Mock-only live model check",
                            provider_id="mock", model_name="mock", config_snapshot={})
                        run.status, run.owner, run.owner_generation = "running", "goal-verify", 1
                        run.lease_until = datetime.now(UTC) + timedelta(minutes=20)
                        session_id, run_id = parent.id, run.id
                    manager = DelegationManager(db, cfg, session_id, run_id)
                    record = await manager.delegate("live-pi-once", DelegationInput(
                        objective="仅进行离线模型工具循环验收：先调用 mock_observe 记录 synthetic alpha；读到该工具返回后，再调用一次 mock_observe 记录 synthetic beta；最后用一句中文总结。禁止把两次调用放在同一轮。",
                        background="这是合成验收数据，没有真实目标或网络工具。mock_observe 只回显合成观察。",
                        completion_requirements=["分两轮分别调用 Mock 工具", "总结不得声称真实目标已验证"]),
                        owner="goal-verify", generation=1, control_version=0)
                    report["delegation_id"] = str(record.id)
                    deadline = asyncio.get_running_loop().time() + 600
                    while record.state not in {"completed", "failed", "cancelled", "rejected"}:
                        if asyncio.get_running_loop().time() > deadline:
                            await manager.cancel(record.id)
                            raise RuntimeError("验收等待超时，已记录取消意图")
                        record = await manager.wait(record.id, 20)
                    report.update(status=str(record.state), task_id=record.a2a_task_id, context_id=str(record.context_id), diagnostic=record.diagnostic)
                    async with db.session() as session:
                        calls = (await session.execute(text("SELECT sequence,metric FROM ultra_worker.model_calls WHERE request_id=:id ORDER BY sequence"), {"id": record.id})).all()
                        report["model_calls"] = [{"sequence": r.sequence, "metric": r.metric} for r in calls]
                        if record.result_artifact_id:
                            artifact = await session.get(ArtifactRow, record.result_artifact_id)
                            assert artifact
                            result = WorkerResult.model_validate_json(ArtifactStore(cfg).read_bytes(artifact.relative_key))
                            report.update(model_usage=result.model_usage.model_dump() if result.model_usage else None,
                                mock_tool_observations=len(result.observations), summary=result.summary)
                    report["passed"] = record.state == "completed" and len(calls) >= 3 and report.get("mock_tool_observations") == 2
                finally:
                    await db.close()
                process.terminate()
                process.wait(timeout=10)
                process = None
    except (RuntimeError, OSError, ValueError, psycopg.Error) as exc:
        report["error_type"] = type(exc).__name__
        report["passed"] = False
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if created:
            with psycopg.connect(admin, autocommit=True, connect_timeout=5) as connection:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        report["finished_at"] = datetime.now(UTC).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: report.get(k) for k in ("status", "passed", "model_usage", "error_type")}, ensure_ascii=False))
    return report.get("passed") is True


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_event_loop_policy()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true", required=True, help="显式确认本次调用真实模型（仅 Mock 工具）")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
