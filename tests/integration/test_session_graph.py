"""双图真实 PostgreSQL 事务与恢复；无真实靶场。"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.session_graph import read_graph, update_graph
from agent_workbench.persistence.tables import SessionGraphRow
from tests.integration.test_natural_completion_safety import setup_run

pytestmark = pytest.mark.integration
adapter = TypeAdapter(list[GraphOperation])


async def fixture(database, unique_name):
    run, task, generation = await setup_run(database, unique_name)
    return SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=task.id,
        owner="natural",
        owner_generation=generation,
        control_version=run.control_version,
        settings=SimpleNamespace(
            graph_sync_interval=5,
            graph_bounded_review=False,
            graph_inject_every_round=False,
            graph_attack_enabled=True,
            graph_context_chars=24000,
        ),
    )


async def present(box):
    """模拟 ContextBuilder 已向请求展示系统生成的页面。"""
    from agent_workbench.domain.graph_sync import present_page

    async with box.db.transaction() as db:
        await present_page(db, box, box.graph_sync_snapshot["page_id"])


def operations():
    return adapter.validate_python(
        [
            {
                "op": "add_node",
                "graph": "information",
                "temp_id": "host",
                "data": {"label": "入口"},
            },
            {
                "op": "add_node",
                "graph": "attack",
                "temp_id": "state",
                "data": {"summary": "入口可达", "entity_refs": ["host"]},
            },
        ]
    )


async def test_graph_persistence_replay_conflict_and_isolation(database, unique_name):
    box = await fixture(database, unique_name)
    async with database.session() as db:
        assert (await read_graph(db, box.session_id))["revision"] == 0
    for _ in range(2):
        async with database.transaction() as db:
            result = await update_graph(db, box, "call-1", 0, operations())
            assert result["revision"] == 1
    async with database.session() as db:
        graph = await read_graph(db, box.session_id)
        assert len(graph["information_graph"]["nodes"]) == 1
        assert (await read_graph(db, uuid4()))["revision"] == 0
    with pytest.raises(ValueError, match="版本冲突"):
        async with database.transaction() as db:
            await update_graph(db, box, "call-2", 0, operations())
    invalid = adapter.validate_python(
        [
            {
                "op": "add_edge",
                "graph": "information",
                "temp_id": "bad",
                "data": {
                    "from_id": "missing",
                    "to_id": "missing",
                    "relation": "猜测",
                    "status": "hypothesis",
                },
            }
        ]
    )
    with pytest.raises(ValueError, match="不存在"):
        async with database.transaction() as db:
            await update_graph(db, box, "call-3", 1, invalid)
    async with database.session() as db:
        assert (await db.get(SessionGraphRow, box.session_id)).revision == 1


async def test_attempt_self_loop_sources_history_and_atomic_ack(database, unique_name):
    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.persistence.tables import SessionGraphReceiptRow
    from agent_workbench.tools.operation import OperationRegistry

    box = await fixture(database, unique_name)
    async with database.transaction() as db:
        op = await OperationRegistry(db).prepare(
            run_id=box.run_id,
            session_id=box.session_id,
            tool_call_id="real",
            tool_name="fixture",
            arguments={},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
        op.row.status = "succeeded"
        source = "operation:" + str(op.operation_id)
        initial = await update_graph(db, box, "initial", 0, operations())
    state_id = initial["id_mapping"]["state"]
    attempt = adapter.validate_python(
        [
            {
                "op": "add_edge",
                "graph": "attack",
                "temp_id": "attempt",
                "data": {
                    "from_id": state_id,
                    "to_id": state_id,
                    "action": "无预期效果",
                    "outcome": "negative",
                    "source_refs": [source],
                },
            }
        ]
    )
    from agent_workbench.domain.graph_sync import graph_context

    box.settings.graph_inject_every_round = True
    page = await graph_context(box, None)
    await present(box)
    async with database.transaction() as db:
        result = await update_graph(db, box, "attempt", 1, attempt, box.graph_sync_snapshot)
        assert result["reviewed_seq"] == page["through_seq"]
    bad = adapter.validate_python(
        [
            {
                "op": "add_node",
                "graph": "information",
                "temp_id": "fake",
                "data": {"label": "伪造", "source_refs": ["artifact:" + str(uuid4())]},
            }
        ]
    )
    with pytest.raises(ValueError, match="不存在"):
        async with database.transaction() as db:
            await update_graph(
                db, box, "bad", 2, bad, {"complete": True, "through_seq": 100, "rounds": 5}
            )
    async with database.session() as db:
        row = await db.get(SessionGraphRow, box.session_id)
        assert row.reviewed_seq == page["through_seq"] and row.revision == 2
        receipt = await db.get(
            SessionGraphReceiptRow, {"session_id": box.session_id, "request_id": "attempt"}
        )
        assert receipt.receipt["after"]["attack_graph"]["edges"][0]["outcome"] == "negative"


@pytest.mark.parametrize("fault", ["cancel", "lease", "control", "owner"])
async def test_graph_commit_rechecks_control(database, unique_name, fault):
    from datetime import UTC, datetime, timedelta

    from agent_workbench.persistence.tables import RunRow

    box = await fixture(database, unique_name)
    async with database.transaction() as db:
        run = await db.get(RunRow, box.run_id)
        if fault == "cancel":
            run.status = "cancelled"
        if fault == "lease":
            run.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        if fault == "control":
            run.control_version += 1
        if fault == "owner":
            run.owner = "another"
    with pytest.raises(ValueError, match="租约"):
        async with database.transaction() as db:
            await update_graph(db, box, "late", 0, operations())


async def test_five_rounds_empty_ack_ignore_retry_and_job_events(database, unique_name):
    from langchain_core.messages import AIMessage

    from agent_workbench.contracts.events import EventType
    from agent_workbench.domain.graph_sync import graph_context
    from agent_workbench.observability.events import append_event

    box = await fixture(database, unique_name)
    box.settings.graph_inject_every_round = False
    box.settings.graph_attack_enabled = True
    box.settings.graph_context_chars = 24000
    box.graph_sync_snapshot = None
    async with database.transaction() as db:
        await append_event(
            db,
            session_id=box.session_id,
            run_id=box.run_id,
            event_type=EventType.TOOL_COMPLETED,
            payload={
                "scope": "job_wakeup",
                "summary": [{"job_id": str(uuid4()), "state": "unknown"}],
            },
        )
    message = None
    for i in range(5):
        message = AIMessage(
            content="",
            id=f"decision-{i}",
            tool_calls=[{"id": f"c-{i}", "name": "fixture", "args": {}}],
        )
        view = await graph_context(box, message)
        if i < 4:
            assert "graph" not in view
        else:
            assert "graph" in view and view["through_seq"] > 0
    assert message is not None
    snapshot = dict(cast(dict[str, Any], box.graph_sync_snapshot))
    await present(box)
    assert (await graph_context(box, message))["rounds"] == 5  # restored boundary/retry
    assert box.graph_sync_snapshot is None  # ignored delivery has finite reminder interval
    async with database.transaction() as db:
        await update_graph(db, box, "empty", 0, [], snapshot)
    view = await graph_context(box, message)
    assert view["reviewed_seq"] == snapshot["through_seq"]
    # Graph-only rounds never grow the exploration counter.
    graph_round = AIMessage(
        content="", id="graph-only", tool_calls=[{"id": "g", "name": "get_graph", "args": {}}]
    )
    assert (await graph_context(box, graph_round))["rounds"] == 5


async def test_official_agent_tools_and_natural_end(database, unique_name):
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage

    from agent_workbench.agent.context import RunContext
    from agent_workbench.tools.session_graph import GRAPH_TOOLS
    from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

    box = await fixture(database, unique_name)
    box.settings.graph_attack_enabled = True
    box.graph_sync_snapshot = None
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "update_graph",
                {"expected_revision": 0, "operations": [x.model_dump() for x in operations()]},
                "graph",
            ),
            tool_call("get_graph", {}, "recall"),
            final_text("任务已结束，无需同步门禁"),
        ]
    )
    graph = create_agent(model, tools=GRAPH_TOOLS, context_schema=RunContext)
    # RunContext requires only trusted runtime dependencies; no IDs in model schema.
    context = RunContext(toolbox=cast(Any, box))
    result = await graph.ainvoke({"messages": [HumanMessage(content="模拟任务")]}, context=context)
    assert result["messages"][-1].content == "任务已结束，无需同步门禁"
    assert "入口" in result["messages"][-2].content


async def test_checkpoint_reconnect_five_rounds_sync_and_compaction(database, unique_name):
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.agent.context import RunContext
    from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.tools.session_graph import GRAPH_TOOLS
    from tests.integration.test_compaction_persistence import _graph
    from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

    box = await fixture(database, unique_name)
    box.settings = database.settings.model_copy(update={"graph_bounded_review": False})
    box.graph_sync_snapshot = None
    box.working_context = None
    views = []

    @tool
    async def fixture_read() -> str:
        """本地确定性读取，不访问网络。"""
        views.append(dict(box.working_context["session_memory"]))
        return "ordinary data"

    responses = [tool_call("fixture_read", {}, f"read-{i}") for i in range(5)]
    responses += [
        tool_call("update_graph", {"expected_revision": 0, "operations": []}, "ack"),
        final_text("自然结束"),
    ]

    class DeliveredPageModel(ScriptedChatModel):
        def _generate(self, messages, *args, **kwargs):
            if box.graph_sync_snapshot:
                for call in self.responses[len(self.seen_message_counts)].tool_calls:
                    if call["name"] == "update_graph":
                        call["args"]["page_id"] = box.graph_sync_snapshot["page_id"]
            return super()._generate(messages, *args, **kwargs)

    from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware

    model = DeliveredPageModel(responses=responses)
    config = cast(Any, {"configurable": {"thread_id": f"dual-{box.session_id}"}})
    result: Any = None
    for index in range(7):
        async with open_checkpointer_connection(database.settings) as connection:
            graph = create_agent(
                model,
                tools=[fixture_read, *GRAPH_TOOLS],
                middleware=[WorkingContextMiddleware(), ContextBuilderMiddleware()],
                context_schema=RunContext,
                checkpointer=AsyncPostgresSaver(connection),
                interrupt_after=["tools"],
            )
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content="模拟任务")]} if index == 0 else None,
                config,
                context=RunContext(toolbox=cast(Any, box)),
            )
    assert all("graph" not in view for view in views)
    assert result["messages"][-1].content == "自然结束"
    async with database.session() as db:
        saved = await db.get(SessionGraphRow, box.session_id)
        assert saved.revision == 1 and saved.reviewed_rounds == 5
    # Actual GuardedSummarization archives/replaces a separate checkpoint history;
    # neither graph persistence nor its source cursor is owned by messages.
    async with open_checkpointer_connection(database.settings) as connection:
        compact = _graph("摘要中的推测不写入图", AsyncPostgresSaver(connection))
        await compact.ainvoke(
            {
                "messages": [HumanMessage(content=f"old {i}") for i in range(8)],
                "objective": "模拟任务",
            },
            cast(Any, {"configurable": {"thread_id": f"compact-{box.session_id}"}}),
        )
    async with database.session() as db:
        assert (await read_graph(db, box.session_id))["revision"] == 1


async def test_concurrent_revision_and_crash_rollback(database, unique_name):
    import asyncio

    box = await fixture(database, unique_name)

    async def write(key):
        async with database.transaction() as db:
            return await update_graph(db, box, key, 0, operations())

    outcomes = await asyncio.gather(write("one"), write("two"), return_exceptions=True)
    assert sum(isinstance(x, ValueError) for x in outcomes) == 1
    with pytest.raises(RuntimeError):
        async with database.transaction() as db:
            await update_graph(
                db, box, "crash", 1, [], {"complete": True, "through_seq": 99, "rounds": 5}
            )
            raise RuntimeError("fault before commit")
    async with database.session() as db:
        saved = await db.get(SessionGraphRow, box.session_id)
        assert saved.revision == 1 and saved.reviewed_seq == 0


async def test_cross_session_operation_and_modified_replay_rejected(database, unique_name):
    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.tools.operation import OperationRegistry

    first = await fixture(database, unique_name + "-first")
    second = await fixture(database, unique_name + "-second")
    async with database.transaction() as db:
        source = await OperationRegistry(db).prepare(
            run_id=second.run_id,
            session_id=second.session_id,
            tool_call_id="source",
            tool_name="fixture",
            arguments={},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
        ref = "operation:" + str(source.operation_id)
    foreign = adapter.validate_python(
        [
            {
                "op": "add_node",
                "graph": "information",
                "temp_id": "e",
                "data": {"label": "不允许借用", "source_refs": [ref]},
            }
        ]
    )
    with pytest.raises(ValueError, match="不属于"):
        async with database.transaction() as db:
            await update_graph(db, first, "cross", 0, foreign)
    async with database.transaction() as db:
        await update_graph(db, first, "original", 0, operations())
    with pytest.raises(ValueError, match="参数与原调用不同"):
        async with database.transaction() as db:
            await update_graph(db, first, "original", 0, [])


async def test_late_job_result_gets_new_sequence_without_model_retry_count(database, unique_name):
    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.domain.graph_sync import graph_context
    from agent_workbench.persistence.repositories import ToolJobRepository
    from agent_workbench.persistence.tables import ToolJobRow
    from agent_workbench.tools.operation import OperationRegistry

    box = await fixture(database, unique_name)
    box.settings.graph_inject_every_round = True
    box.settings.graph_attack_enabled = True
    box.settings.graph_context_chars = 24000
    job_id = uuid4()
    async with database.transaction() as db:
        op = await OperationRegistry(db).prepare(
            run_id=box.run_id,
            session_id=box.session_id,
            tool_call_id="async",
            tool_name="fixture",
            arguments={},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
        op.row.job_id = job_id
        await ToolJobRepository(db).upsert(
            job_id=job_id, operation_id=op.operation_id, executor_id="fixture", state="unknown"
        )
    first = await graph_context(box, None)
    assert any(item.get("job_state") == "unknown" for item in first["pending"])
    await present(box)
    async with database.transaction() as db:
        await update_graph(db, box, "ack-unknown", 0, [], box.graph_sync_snapshot)
        job = await db.get(ToolJobRow, job_id)
        job.state = "succeeded"
        job.remote_revision += 1
    later = await graph_context(box, None)
    assert later["through_seq"] > first["through_seq"]
    assert later["rounds"] == 0
    assert any(item.get("job_state") == "succeeded" for item in later["pending"])


async def test_retention_preserves_unreviewed_graph_sources(database, unique_name):
    from datetime import UTC, datetime, timedelta

    from agent_workbench.contracts.events import EventType
    from agent_workbench.maintenance.retention import retention_candidates
    from agent_workbench.observability.events import append_event
    from agent_workbench.persistence.tables import EventRow, RunRow

    box = await fixture(database, unique_name)
    async with database.transaction() as db:
        await update_graph(db, box, "start", 0, [])
        event = await append_event(
            db,
            session_id=box.session_id,
            run_id=box.run_id,
            event_type=EventType.TOOL_COMPLETED,
            payload={"status": "unknown"},
        )
        await append_event(
            db,
            session_id=box.session_id,
            run_id=box.run_id,
            event_type=EventType.TOOL_COMPLETED,
            payload={"status": "succeeded"},
        )
        run = await db.get(RunRow, box.run_id)
        run.status = "completed"
    async with database.transaction() as db:
        cutoff = datetime.now(UTC) + timedelta(days=1)
        row = (
            await db.execute(
                __import__("sqlalchemy").select(EventRow).where(EventRow.event_id == event.event_id)
            )
        ).scalar_one()
        assert row.id not in await retention_candidates(
            db, before=cutoff, limit=1000, session_id=box.session_id
        )
        graph = await db.get(SessionGraphRow, box.session_id)
        graph.reviewed_seq = event.session_seq
        await db.flush()
        assert row.id in await retention_candidates(
            db, before=cutoff, limit=1000, session_id=box.session_id
        )


async def test_operation_digest_artifact_read_and_multiple_sources(database, unique_name):
    import json

    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.contracts.events import EventType
    from agent_workbench.domain.graph_sync import graph_context
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.observability.events import append_event
    from agent_workbench.persistence.repositories import ArtifactRepository, ToolJobRepository
    from agent_workbench.persistence.tables import ExecutionResourceRow
    from agent_workbench.tools.evidence import read_artifact
    from agent_workbench.tools.history import search_history
    from agent_workbench.tools.operation import OperationRegistry

    box = await fixture(database, unique_name)
    box.settings = database.settings.model_copy(update={"graph_inject_every_round": True})
    box.artifacts = ArtifactStore(database.settings)
    refs = []
    operations_created = []
    job_ids = []
    async with database.transaction() as db:
        for index, state in enumerate(["succeeded", "unknown"]):
            op = await OperationRegistry(db).prepare(
                run_id=box.run_id,
                session_id=box.session_id,
                tool_call_id=f"c{index}",
                tool_name="fixture_read",
                arguments={},
                effect_class=ToolEffectClass.READ_ONLY,
                policy_revision=1,
                scope_revision=1,
            )
            op.row.status = state
            op.row.result_snapshot = {
                "summary": "已执行读取",
                "data": {
                    "status_code": 200,
                    "headers": {"X-Fixture": "existing-result", "Authorization": "secret-value"},
                },
            }
            job_id = uuid4()
            job_ids.append(job_id)
            op.row.job_id = job_id
            await ToolJobRepository(db).upsert(
                job_id=job_id, operation_id=op.operation_id, executor_id="fixture", state=state
            )
            db.add(
                ExecutionResourceRow(
                    resource_id=job_id,
                    session_id=box.session_id,
                    origin_run_id=box.run_id,
                    origin_operation_id=op.operation_id,
                    executor_id="fixture",
                    purpose="读取已有本地记录",
                    state=state,
                    exit_code=0 if index == 0 else None,
                    termination_reason="exited" if index == 0 else None,
                    quota={"output_bytes": 123, "dropped_bytes": 7 if index == 0 else None},
                )
            )
            stored = box.artifacts.write_bytes(
                b'{"token":"secret-value","value":"ordinary"}' if index == 0 else b"\x00\xff",
                media_type="application/json; charset=utf-8"
                if index == 0
                else "application/octet-stream",
            )
            artifact = await ArtifactRepository(db).create(
                artifact_id=uuid4(),
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
                relative_key=stored.relative_key,
                session_id=box.session_id,
                source_operation_id=op.operation_id,
                sensitivity="internal",
                availability="available",
                captured_at=stored.captured_at,
            )
            refs.append((artifact, stored))
            operations_created.append(op.operation_id)
            for status in ["running", state]:
                await append_event(
                    db,
                    session_id=box.session_id,
                    run_id=box.run_id,
                    event_type=EventType.TOOL_COMPLETED,
                    payload={"tool_call_id": f"c{index}", "status": status},
                )
        initial = await update_graph(db, box, "initial", 0, operations())
    view = await graph_context(box, None)
    assert len(view["pending"]) == 2
    for index, item in enumerate(view["pending"]):
        assert item["operation_ref"] == f"operation:{operations_created[index]}"
        assert item["job_id"] == str(job_ids[index])
        assert item["job_state"] == ["succeeded", "unknown"][index]
        assert item["operation_status"] == ["succeeded", "unknown"][index]
        assert item["artifact_refs"] == [f"artifact:{refs[index][0].id}"]
        assert len(item["event_seqs"]) >= 2
        assert "outcome" not in item
        assert item["purpose"] == "读取已有本地记录"
        assert item["execution"]["output_bytes_total"] == 123
        assert item["execution"]["output_locator"] == {
            "tool": "read_job_output",
            "job_id": str(job_ids[index]),
        }
        assert "stdout_bytes" not in item["execution"]
    assert view["pending"][1]["certainty"] == "uncertain"
    assert view["pending"][0]["certainty"] == "terminal"
    await present(box)
    async with database.session() as db:
        assert (await read_graph(db, box.session_id))["attack_graph"]["edges"] == []
    attempt = adapter.validate_python(
        [
            {
                "op": "add_edge",
                "graph": "attack",
                "temp_id": "combined",
                "data": {
                    "from_id": initial["id_mapping"]["state"],
                    "to_id": initial["id_mapping"]["state"],
                    "action": "多来源调查，未观察到预期效果",
                    "outcome": "negative",
                    "source_refs": [f"operation:{key}" for key in operations_created],
                },
            }
        ]
    )
    async with database.transaction() as db:
        result = await update_graph(db, box, "combined", 1, attempt, box.graph_sync_snapshot)
        assert result["reviewed_seq"] == view["through_seq"]
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box), state={})
    for index, (artifact, stored) in enumerate(refs):
        before = box.artifacts.read_bytes(stored.relative_key)
        result = json.loads(
            await cast(Any, read_artifact).coroutine(runtime=runtime, artifact_id=str(artifact.id))
        )
        assert "secret-value" not in json.dumps(result)
        if index == 0:
            assert "ordinary" in result["data"]["text"]
        else:
            assert result["data"]["text"] == ""
            assert "Operation" in result["data"]["encoding_note"]
        assert box.artifacts.read_bytes(stored.relative_key) == before
    history = await cast(Any, search_history).coroutine(
        runtime=runtime, kind="operation", query=str(operations_created[0])
    )
    assert "existing-result" in history and "secret-value" not in history
