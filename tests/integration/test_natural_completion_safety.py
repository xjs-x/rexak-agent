"""自然结束只受运行控制与未决执行约束，不审查模型业务结论。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

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


async def test_snapshot_exposes_saved_final_response_without_config_or_stale_resume(database, unique_name):
    from agent_workbench.persistence.tables import ModelCallRow

    call_id = uuid4()
    with TestClient(create_app()) as client:
        sid, rid, headers = create_task(client, unique_name)
        async with database.transaction() as db:
            run = await db.get(RunRow, rid)
            run.status = "waiting_tool"
            run.config_snapshot = {**run.config_snapshot,
                "completion_result_artifact_id": str(uuid4()),
                "completion_result_model_call_id": str(call_id)}
            db.add(ModelCallRow(model_call_id=call_id, logical_call_id=uuid4(),
                run_id=rid, session_id=sid, phase="agent", provider_id="fixture",
                model_name="fixture", submitted_at=datetime.now(UTC)))
        before = client.get(f"/api/sessions/{sid}/snapshot", headers=headers)
        assert before.status_code == 200
        view = before.json()["runs"][0]
        assert view["status"] == "waiting_tool" and view["model_response_saved"]
        assert "config_snapshot" not in view
        # 同 Run 新决策尚未结束，旧报告指针不能继续冒充本次模型已经结束。
        async with database.transaction() as db:
            db.add(ModelCallRow(model_call_id=uuid4(), logical_call_id=uuid4(),
                run_id=rid, session_id=sid, phase="agent", provider_id="fixture",
                model_name="fixture", submitted_at=datetime.now(UTC) + timedelta(seconds=1)))
        after = client.get(f"/api/sessions/{sid}/snapshot", headers=headers)
        assert after.status_code == 200
        assert not after.json()["runs"][0]["model_response_saved"]


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
    runner = AgentRunner(database=database, owner="natural")
    runtime = runtime_for(run)
    # Reconstruct the runner as on recovery; only inspect committed END, never invoke a model.
    for _ in range(2):
        result = await runner._resolve_after_segment(runtime, run, task, SpanRecorder())
        assert result == (RunStatus.WAITING_TOOL, None)
        runner = AgentRunner(database=database, owner="natural")
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow

    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        artifact = await db.get(
            ArtifactRow, UUID(saved.config_snapshot["completion_result_artifact_id"])
        )
        assert (
            ArtifactStore(database.settings).read_bytes(artifact.relative_key).decode()
            == (await runtime.graph.aget_state({})).values["messages"][-1].text
        )
        assert (await db.get(ToolJobRow, job_id)).state == state
    async with database.transaction() as db:
        job = await db.get(ToolJobRow, job_id)
        job.state = "succeeded"
        job.sync_status = "in_sync"
        from agent_workbench.persistence.tables import ToolOperationRow

        (await db.get(ToolOperationRow, op.operation_id)).status = "succeeded"
    assert (await runner._resolve_after_segment(runtime, run, task, SpanRecorder()))[
        0
    ] == RunStatus.COMPLETED


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


async def test_unknown_final_checkpoint_reconnect_and_conversation_read(database, unique_name):
    """真实 checkpoint 重连后投影同一回复；不重新调模型，不改变未知执行。"""
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.agent.state import AgentGraphState
    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.persistence.tables import ToolJobRow, ToolOperationRow
    from tests.support.scripted_model import ScriptedChatModel, final_text

    run, task, _ = await setup_run(database, unique_name)
    async with database.transaction() as db:
        operation = await OperationRegistry(db).prepare(
            run_id=run.id,
            session_id=task.id,
            tool_call_id="unknown-final",
            tool_name="fixture",
            arguments={},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
        operation.row.status = "unknown"
        job_id = uuid4()
        operation.row.job_id = job_id
        await ToolJobRepository(db).upsert(
            job_id=job_id,
            operation_id=operation.operation_id,
            executor_id="fixture",
            state="unknown",
            sync_status="uncertain",
        )

    answer = "本地验收报告：执行结果仍不确定，未重复执行。"
    model = ScriptedChatModel(responses=[final_text(answer)])
    config = cast(Any, {"configurable": {"thread_id": task.thread_id}})
    async with open_checkpointer_connection(database.settings) as connection:
        graph = create_agent(
            model, state_schema=AgentGraphState, checkpointer=AsyncPostgresSaver(connection)
        )
        await graph.ainvoke(
            cast(
                Any,
                {
                    "messages": [HumanMessage(content="仅使用本地脚本结果")],
                    "active_run_id": str(run.id),
                    "dispatch_control_version": run.control_version,
                },
            ),
            config,
        )
        committed_id = (await graph.aget_state(config)).config.get("configurable", {})[
            "checkpoint_id"
        ]
    assert len(model.seen_message_counts) == 1

    for _ in range(2):
        # Empty script fails if a model is unexpectedly invoked after reconnect.
        restored_model = ScriptedChatModel(responses=[])
        async with open_checkpointer_connection(database.settings) as connection:
            graph = create_agent(
                restored_model,
                state_schema=AgentGraphState,
                checkpointer=AsyncPostgresSaver(connection),
            )
            runtime = cast(Any, SimpleNamespace(graph=graph))
            result = await AgentRunner(database=database, owner="natural")._resolve_after_segment(
                runtime, run, task, SpanRecorder()
            )
            assert result == (RunStatus.WAITING_TOOL, None)
            assert (await graph.aget_state(config)).config.get("configurable", {})[
                "checkpoint_id"
            ] == committed_id
        assert restored_model.seen_message_counts == []
        async with database.session() as db:
            assert (await db.get(ToolJobRow, job_id)).state == "unknown"
            assert (await db.get(ToolOperationRow, operation.operation_id)).status == "unknown"
        with TestClient(create_app()) as client:
            assert client.post("/api/session").status_code == 200
            page = client.get(f"/api/sessions/{task.id}/conversation")
            assert page.status_code == 200, page.text
            reports = [item for item in page.json()["entries"] if item["source"] == "result"]
            assert len(reports) == 1 and reports[0]["available"]
            report = client.get(
                f"/api/sessions/{task.id}/conversation/result/{reports[0]['id']}/text"
            )
            assert report.status_code == 200, report.text
            assert answer in report.text
