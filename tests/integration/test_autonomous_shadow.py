"""隔离 PostgreSQL + 完整 Runner + AsyncPostgresSaver；模型/Executor 均离线替身。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from experiments.history_action_autonomous_shadow_v1.fixture import policy_for
from experiments.history_action_trusted_shadow_v1.fixture import Executor, parsed, tool_arguments
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.instruction_scope import sync_instruction_scope
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    ArtifactRow,
    RunRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.tools.http import http_request
from agent_workbench.tools.shadow_test_profile import run_http_test_profile
from tests.integration.test_instruction_scope import create_task
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import final_text, tool_call

pytestmark = pytest.mark.integration


class MockExecutor(Executor):
    def __init__(self):
        super().__init__()
        self.closed = 0

    async def start(self):
        pass

    async def close(self):
        self.closed += 1  # 跨 Runner 保留 mock 远端台账；最后由测试关闭 MockTransport。


class NoTunnel:
    def __init__(self, *args, **kwargs):
        pass

    async def close(self):
        pass


async def run_case(
    database,
    name,
    root,
    monkeypatch,
    *,
    enabled=True,
    restart=False,
    restart_process=False,
    spec=None,
    observer_fault=None,
):
    from agent_workbench.agent import factory
    from agent_workbench.runtime import runner as runner_module

    executor = MockExecutor()
    child_jobs = child_closed = 0
    spec = spec or {}
    baseline = parsed(tool_arguments(**spec.get("request", {}))).model_dump(exclude={"note"})
    candidate = parsed(
        tool_arguments(**{**spec.get("request", {}), **spec.get("next_request", {})})
    ).model_dump(exclude={"note"})
    next_tool = "http_request" if spec.get("navigation") else "run_http_test_profile"
    next_arguments = tool_arguments() if spec.get("navigation") else {"profile_id": "next"}
    steps = [
        lambda _: tool_call("run_http_test_profile", {"profile_id": "baseline"}, "first"),
        lambda _: tool_call(next_tool, next_arguments, "repeat"),
        lambda _: final_text("controlled offline execution finished"),
    ]
    model = SteppedModel(steps=steps)
    monkeypatch.setenv("XIAOMI_API_KEY", "synthetic-unused-no-network")
    monkeypatch.setattr(factory, "build_chat_model", lambda *_a, **_kw: model)
    monkeypatch.setattr(
        factory, "build_tools", lambda *_a, **_kw: [run_http_test_profile, http_request]
    )
    monkeypatch.setattr(runner_module, "HttpExecutorClient", lambda *_a, **_kw: executor)
    monkeypatch.setattr(runner_module, "SshTunnel", NoTunnel)
    root.mkdir(parents=True)
    settings = database.settings.model_copy(
        update={
            "executor_id": "offline-executor",
            "history_action_shadow_enabled": enabled,
            "history_action_shadow_grants_path": None,
            "history_action_shadow_context_path": root / "task-context.json",
            "segment_decision_rounds": 1,
            "history_action_shadow_timeout_seconds": 2,
        }
    )
    try:
        with TestClient(create_app()) as client:
            sid, rid, headers = create_task(
                client, name, objective="test https://contract.invalid/check"
            )
            async with database.transaction() as db:
                # 此夹具回归旧 Shadow Only 接缝，不启用新生产辅助模式。
                saved_run = await db.get(RunRow, rid)
                legacy_config = dict(saved_run.config_snapshot)
                legacy_config.pop("history_assistance_mode", None)
                saved_run.config_snapshot = legacy_config
                scope = await sync_instruction_scope(db, session_id=sid, run_id=rid)
                session = await db.get(SessionRow, sid)
            box = SimpleNamespace(
                session_id=sid,
                run_id=rid,
                scope_revision=session.scope_snapshot["revision"],
                scope=ScopePolicy(scope),
                policy_revision=1,
                control_version=0,
                executor=executor,
            )
            policy = policy_for(
                box,
                {
                    "baseline": {"request": baseline},
                    "next": {"request": candidate, **spec.get("next_profile", {})},
                },
            )
            settings.history_action_shadow_context_path.write_text(
                json.dumps(policy), encoding="utf-8"
            )
            runner = AgentRunner(database=database, settings=settings, owner="shadow-runtime")
            if observer_fault:
                original_stream = runner._stream_once
                applied = False

                async def inject_after_first(*args, **kwargs):
                    nonlocal applied
                    await original_stream(*args, **kwargs)
                    if not applied:
                        applied = True
                        await observer_fault(sid, rid, database, settings)

                monkeypatch.setattr(runner, "_stream_once", inject_after_first)
            if restart:
                original = runner._stream_once
                paused = False

                async def pause_at_boundary(*args, **kwargs):
                    nonlocal paused
                    await original(*args, **kwargs)
                    if not paused:
                        paused = True
                        response = client.post(
                            f"/api/runs/{rid}/pause",
                            headers={**headers, "Idempotency-Key": str(rid) + "pause"},
                            json={},
                        )
                        assert response.status_code == 202

                monkeypatch.setattr(runner, "_stream_once", pause_at_boundary)
                result = await runner.step(only_run_id=rid)
                assert result is not None and result.status is RunStatus.PAUSED
                response = client.post(
                    f"/api/runs/{rid}/resume",
                    headers={**headers, "Idempotency-Key": str(rid) + "resume"},
                    json={},
                )
                assert response.status_code == 202
                # 控制版本变化需要控制端发布当前上下文；不是逐 ToolCall 批准。
                async with database.session() as db:
                    row = await db.get(RunRow, rid)
                policy = json.loads(settings.history_action_shadow_context_path.read_text())
                policy["control_version"] = row.control_version
                settings.history_action_shadow_context_path.write_text(
                    json.dumps(policy), encoding="utf-8"
                )
                runner = AgentRunner(
                    database=database, settings=settings, owner="recreated-shadow-runtime"
                )
            if restart_process:
                process = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-m",
                        "experiments.history_action_autonomous_shadow_v1.resume_child",
                        str(rid),
                        str(settings.history_action_shadow_context_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stdout, stderr = process.stdout, process.stderr
                assert process.returncode == 0, (stdout[-2000:], stderr[-2000:])
                child = json.loads(stdout.decode("utf-8").splitlines()[-1])
                child_jobs, child_closed = child["physical_jobs"], child["closed"]
                executor.submissions.extend(child["submissions"])
                result = SimpleNamespace(status=RunStatus(child["status"]))
            else:
                result = await runner.step(only_run_id=rid)
            assert result is not None and result.status is RunStatus.COMPLETED, result
            async with database.session() as db:
                operations = list(
                    (
                        await db.scalars(
                            select(ToolOperationRow).where(ToolOperationRow.run_id == rid)
                        )
                    ).all()
                )
                jobs = list(
                    (
                        await db.scalars(
                            select(ToolJobRow).where(
                                ToolJobRow.operation_id.in_([o.operation_id for o in operations])
                            )
                        )
                    ).all()
                )
                artifacts = list(
                    (
                        await db.scalars(select(ArtifactRow).where(ArtifactRow.session_id == sid))
                    ).all()
                )
                task = await db.get(SessionRow, sid)
            async with open_checkpointer_connection(settings) as conn:
                checkpoint = await AsyncPostgresSaver(conn).aget_tuple(
                    cast(Any, {"configurable": {"thread_id": task.thread_id}})
                )
            assert checkpoint is not None
            values = checkpoint.checkpoint["channel_values"]
            tools = [m for m in values["messages"] if isinstance(m, ToolMessage)]
            assert [m.tool_call_id for m in tools] == ["first", "repeat"]
            assert len(operations) == len(jobs) == len(executor.jobs) + child_jobs == 2
            assert all(o.status == "succeeded" for o in operations)
            assert all(j.state == "succeeded" for j in jobs)
            assert all(
                any(j.operation_id == o.operation_id and j.job_id == o.job_id for j in jobs)
                for o in operations
            )
            shadow_root = Path(settings.data_root) / "history_action_shadow" / str(sid)
            events = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in (shadow_root / "events").glob("*.json")
            ]
            if enabled and not restart and not spec and observer_fault is None:
                assert len(events) == 1, list(shadow_root.rglob("*.json"))
            if not enabled:
                assert not shadow_root.exists()
            return {
                "operations": operations,
                "jobs": jobs,
                "artifacts": artifacts,
                "checkpoint": values,
                "events": events,
                "submissions": executor.submissions,
                "model": model,
                "closed": executor.closed + child_closed,
                "shadow_root": shadow_root,
                "settings": settings,
                "scope": scope,
            }
    finally:
        await executor.client.aclose()


async def test_real_postgres_runner_autonomous_history_loop(
    database, unique_name, tmp_path, monkeypatch
):
    result = await run_case(database, unique_name, tmp_path / "run", monkeypatch)
    assert len(result["events"]) == 1 and result["closed"] == 1


async def test_real_saver_runner_pause_recreate_resume(
    database, unique_name, tmp_path, monkeypatch
):
    result = await run_case(database, unique_name, tmp_path / "resume", monkeypatch, restart=True)
    assert result["closed"] == 2


async def test_real_runner_off_on_business_equivalence(
    database, unique_name, tmp_path, monkeypatch
):
    from experiments.history_action_autonomous_shadow_v1.projections import (
        business_view,
        differences,
    )

    off = await run_case(
        database, unique_name + "off", tmp_path / "off", monkeypatch, enabled=False
    )
    on = await run_case(database, unique_name + "on", tmp_path / "on", monkeypatch)
    a, b = business_view(off), business_view(on)
    assert differences(a, b) == [], differences(a, b)[:30]


@pytest.mark.parametrize("fault", ["disk", "observer_timeout"])
async def test_real_runner_shadow_faults_preserve_original_business(
    database, unique_name, tmp_path, monkeypatch, fault
):
    from experiments.history_action_autonomous_shadow_v1.projections import (
        business_view,
        differences,
    )

    from agent_workbench.tools import history_action_shadow as module

    off = await run_case(
        database, unique_name + "off", tmp_path / "off", monkeypatch, enabled=False
    )
    if fault == "disk":

        def failed_write(*_args, **_kwargs):
            raise OSError("independent observation store unavailable")

        monkeypatch.setattr(module.ShadowService, "_write", failed_write)
    else:

        async def timeout_read(*_args):
            raise TimeoutError("offline observation query deadline")

        monkeypatch.setattr(module, "read_records", timeout_read)
    on = await run_case(
        database, unique_name + "on", tmp_path / "on", monkeypatch, spec={"fault_expected": True}
    )
    assert on["events"] == []
    assert differences(business_view(off), business_view(on)) == []


async def test_real_persisted_artifact_hash_conflict_abstains(
    database, unique_name, tmp_path, monkeypatch
):
    async def corrupt_own_test_artifact(sid, rid, db_handle, _settings):
        async with db_handle.transaction() as db:
            operations = list(
                (
                    await db.scalars(select(ToolOperationRow).where(ToolOperationRow.run_id == rid))
                ).all()
            )
            assert len(operations) == 1
            row = await db.get(ArtifactRow, operations[0].result_artifact_ids[0])
            assert row.session_id == sid
            row.sha256 = "0" * 64

    result = await run_case(
        database,
        unique_name,
        tmp_path / "hash",
        monkeypatch,
        observer_fault=corrupt_own_test_artifact,
    )
    assert result["events"] == []


async def test_real_postgres_checkpoint_restores_in_new_process(
    database, unique_name, tmp_path, monkeypatch
):
    result = await run_case(
        database, unique_name, tmp_path / "process", monkeypatch, restart=True, restart_process=True
    )
    assert len(result["submissions"]) == 2 and result["closed"] == 2
