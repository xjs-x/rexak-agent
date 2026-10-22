"""自然结束只受运行控制与未决执行约束，不审查模型业务结论。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from agent_workbench.agent.signals import GraphRunAborted, LeaseLostError, StaleResponseError
from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import RunStatus, ToolEffectClass
from agent_workbench.domain.run_state import claim_run, set_run_status
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.repositories import ExecutionResourceRepository, ToolJobRepository
from agent_workbench.persistence.tables import RunRow, SessionRow
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.tools.operation import OperationRegistry
from tests.integration.test_instruction_scope import create_task

pytestmark = pytest.mark.integration


async def setup_run(database, unique_name):
    with TestClient(create_app()) as client:
        session_id, run_id, _ = create_task(client, unique_name)
    async with database.transaction() as db:
        await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
        acquired, generation = await claim_run(
            db, run_id=run_id, owner="natural", lease_seconds=120
        )
        assert acquired
    async with database.session() as db:
        run = await db.get(RunRow, run_id)
        task = await db.get(SessionRow, session_id)
    return run, task, generation


def runtime_for(run, **changes):
    values = {
        "active_run_id": str(run.id),
        "dispatch_control_version": run.control_version,
        "messages": [
            AIMessage(content="条件不足，未发现漏洞；引用不存在的工件不产生真实证据", id="final")
        ],
    }
    values.update(changes)

    async def state(_config):
        return SimpleNamespace(values=values, next=())

    return cast(Any, SimpleNamespace(graph=SimpleNamespace(aget_state=state)))


@pytest.mark.parametrize("fault", ["pause", "cancel", "lease", "control"])
async def test_late_control_changes_block_natural_completion(database, unique_name, fault):
    run, task, _ = await setup_run(database, unique_name)
    runner = AgentRunner(database=database, owner="natural")
    runtime = runtime_for(run)
    async with database.transaction() as db:
        saved = await db.get(RunRow, run.id)
        if fault == "pause":
            saved.status = "pausing"
        elif fault == "cancel":
            saved.status = "cancelling"
        elif fault == "lease":
            saved.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        else:
            saved.control_version += 1
    error = (
        LeaseLostError
        if fault == "lease"
        else StaleResponseError
        if fault == "control"
        else GraphRunAborted
    )
    with pytest.raises(error):
        await runner._resolve_after_segment(runtime, run, task, SpanRecorder())
    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        assert saved.status != "completed"
        assert "completion_result_artifact_id" not in saved.config_snapshot


@pytest.mark.parametrize("fault", ["pause", "cancel", "control", "owner"])
async def test_terminal_commit_rechecks_control_atomically(database, unique_name, fault):
    run, _task, generation = await setup_run(database, unique_name)
    async with database.transaction() as db:
        saved = await db.get(RunRow, run.id)
        if fault in {"pause", "cancel"}:
            saved.status = "pausing" if fault == "pause" else "cancelling"
        elif fault == "control":
            saved.control_version += 1
        else:
            saved.owner = "replacement"
            saved.owner_generation += 1
    runner = AgentRunner(database=database, owner="natural")
    await runner._finalize(
        run.id,
        RunStatus.COMPLETED,
        detail="",
        completion_reason=None,
        expected_generation=generation,
        expected_control_version=run.control_version,
    )
    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        assert (
            saved.status
            == {
                "pause": "paused",
                "cancel": "cancelling",
                "control": "recovering",
                "owner": "running",
            }[fault]
        )
        if fault == "owner":
            assert saved.owner == "replacement"


@pytest.mark.parametrize("state", ["running", "unknown"])
async def test_final_reply_does_not_discard_unsettled_jobs(database, unique_name, state):
    run, task, _ = await setup_run(database, unique_name)
    async with database.transaction() as db:
        op = await OperationRegistry(db).prepare(
            run_id=run.id,
            session_id=task.id,
            tool_call_id="async",
            tool_name="browser_navigate",
            arguments={},
            effect_class=ToolEffectClass.SIDE_EFFECTING,
            policy_revision=1,
            scope_revision=1,
        )
        job_id = uuid4()
        op.row.job_id = job_id
        op.row.status = state
        await ToolJobRepository(db).upsert(
            job_id=job_id, operation_id=op.operation_id, executor_id="fixture", state=state
        )
    result = await AgentRunner(database=database, owner="natural")._resolve_after_segment(
        runtime_for(run), run, task, SpanRecorder()
    )
    assert result == (RunStatus.WAITING_TOOL, None)


async def test_finite_untracked_execution_requires_recovery(database, unique_name):
    run, task, _ = await setup_run(database, unique_name)
    async with database.transaction() as db:
        await ExecutionResourceRepository(db).upsert(
            resource_id=uuid4(),
            session_id=task.id,
            executor_id="fixture",
            kind="command",
            state="running",
            origin_run_id=run.id,
        )
    assert (
        await AgentRunner(database=database, owner="natural")._resolve_after_segment(
            runtime_for(run), run, task, SpanRecorder()
        )
    ) == (RunStatus.RECOVERING, None)


async def test_restart_at_committed_end_archives_once_without_model(database, unique_name):
    run, task, _ = await setup_run(database, unique_name)
    runner = AgentRunner(database=database, owner="natural")
    runtime = runtime_for(run)
    for _ in range(2):
        assert (await runner._resolve_after_segment(runtime, run, task, SpanRecorder()))[
            0
        ] == RunStatus.COMPLETED
    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        assert saved.config_snapshot["completion_result_artifact_id"]


async def test_intermediate_tool_call_and_yield_are_not_final(database, unique_name):
    run, task, _ = await setup_run(database, unique_name)
    runner = AgentRunner(database=database, owner="natural")
    for values in [
        {"segment_yield_reason": "decision_round_limit"},
        {
            "messages": [
                AIMessage(content="中间文本", tool_calls=[{"name": "local", "args": {}, "id": "c"}])
            ]
        },
        {"messages": [ToolMessage(content="结果", tool_call_id="c")]},
    ]:
        assert await runner._resolve_after_segment(
            runtime_for(run, **values), run, task, SpanRecorder()
        ) == (None, None)
