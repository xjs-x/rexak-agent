"""定向验收补充：不请求真实模型或执行岛，只写隔离测试库。"""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.domain.graph_sync import graph_context
from agent_workbench.domain.session_graph import read_graph, update_graph
from agent_workbench.persistence.tables import SessionGraphRow
from tests.integration.test_session_graph import adapter, fixture, present
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

pytestmark = pytest.mark.integration


def decision(index):
    return AIMessage(
        content="",
        id=f"decision-{index}",
        tool_calls=[{"id": f"call-{index}", "name": "fixture_read", "args": {}}],
    )


async def ready(database, unique_name):
    box = await fixture(database, unique_name)
    box.settings = database.settings.model_copy(update={"graph_bounded_review": False})
    box.graph_sync_snapshot = None
    box.working_context = None
    return box


async def test_illegal_review_is_not_retried_after_outcome_commit_and_process_restore(database, unique_name):
    from agent_workbench.domain.graph_sync import review_ready
    from agent_workbench.persistence.tables import SessionGraphReceiptRow

    box = await ready(database, unique_name)
    box.settings.graph_bounded_review = True
    box.graph_review_window_id = "review:restored-illegal"
    async with database.transaction() as db:
        db.add(SessionGraphReceiptRow(session_id=box.session_id,
            request_id=box.graph_review_window_id, digest="review", receipt={
                "status": "active", "attempts": 1, "last_outcome": "disallowed_tool",
                "repair_pending": True,
            }))
    assert not await review_ready(box, box.graph_review_window_id, True)
    async with database.session() as db:
        receipt = await db.get(SessionGraphReceiptRow, {
            "session_id": box.session_id, "request_id": box.graph_review_window_id})
        assert receipt.receipt["status"] == "closed"
        assert receipt.receipt["reason"] == "disallowed_tool"


async def test_sync_context_and_natural_end_without_graph_tools(
    database, unique_name, record_property
):
    box = await ready(database, unique_name)
    captured = []

    @tool
    async def fixture_read() -> str:
        """仅返回可控本地结果。"""
        return "fixture result"

    class Capture(ScriptedChatModel):
        def _generate(self, messages, *args, **kwargs):
            captured.append(str(messages[0].content))
            return super()._generate(messages, *args, **kwargs)

    model = Capture(
        responses=[tool_call("fixture_read", {}, f"r{i}") for i in range(5)]
        + [final_text("无需图工具也可结束")]
    )
    graph = create_agent(
        model,
        tools=[fixture_read],
        context_schema=RunContext,
        middleware=[WorkingContextMiddleware(), ContextBuilderMiddleware()],
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="本地替身任务")]},
        context=RunContext(toolbox=cast(Any, box)),
    )
    assert result["messages"][-1].content == "无需图工具也可结束"
    assert len(captured) == 6
    assert all('"information_graph"' not in text for text in captured[:5])
    assert '"information_graph"' in captured[5] and '"pending"' in captured[5]
    from agent_workbench.models.context_budget import estimate_value

    record_property("ordinary_system_chars", len(captured[0]))
    record_property("sync_system_chars", len(captured[5]))
    record_property("ordinary_system_estimate", estimate_value(captured[0]))
    record_property("sync_system_estimate", estimate_value(captured[5]))
    async with database.session() as db:
        row = await db.get(SessionGraphRow, box.session_id)
        assert row.completed_rounds == 5 and row.revision == 0 and row.reviewed_seq == 0


async def test_pending_pages_failure_and_lost_runtime_snapshot(database, unique_name):
    from agent_workbench.contracts.events import EventType
    from agent_workbench.observability.events import append_event

    box = await ready(database, unique_name)
    box.settings.graph_inject_every_round = True
    box.settings.graph_context_chars = 100000
    async with database.transaction() as db:
        for i in range(205):
            await append_event(
                db,
                session_id=box.session_id,
                run_id=box.run_id,
                event_type=EventType.TOOL_COMPLETED,
                payload={"tool": "fixture", "status": str(i)},
            )
    first = await graph_context(box, None)
    assert len(first["auxiliary_events"]) == 200 and first["remaining_events"] > 0
    snapshot = dict(box.graph_sync_snapshot)
    await present(box)
    bad = adapter.validate_python(
        [
            {
                "op": "add_node",
                "graph": "information",
                "temp_id": "bad",
                "data": {"label": "bad", "source_refs": ["operation:invalid"]},
            }
        ]
    )
    with pytest.raises(ValueError):
        async with database.transaction() as db:
            await update_graph(db, box, "failed", 0, bad, snapshot)
    # A fresh process has no delivery credential; it must not acknowledge old history.
    fresh = SimpleNamespace(**vars(box))
    fresh.graph_sync_snapshot = None
    async with database.transaction() as db:
        result = await update_graph(db, fresh, "after-restart", 0, [], None)
        assert result["reviewed_seq"] == 0
    restored = await graph_context(fresh, None)
    assert [x["seq"] for x in restored["auxiliary_events"]] == [
        x["seq"] for x in first["auxiliary_events"]
    ]
    await present(fresh)
    async with database.transaction() as db:
        await update_graph(db, fresh, "ack-page", 1, [], fresh.graph_sync_snapshot)
    later = await graph_context(fresh, None)
    assert len(later["auxiliary_events"]) == 5
    assert min(x["seq"] for x in later["auxiliary_events"]) > first["through_seq"]


@pytest.mark.parametrize("interval", [2, 5])
async def test_configured_initial_interval(database, unique_name, interval):
    box = await ready(database, unique_name)
    box.settings.graph_sync_interval = interval
    view = {}
    for i in range(interval):
        view = await graph_context(box, decision(i))
    assert view["sync_due"] and "graph" in view
    box.settings.graph_sync_interval = 7
    await graph_context(box, None)
    async with database.session() as db:
        assert (await db.get(SessionGraphRow, box.session_id)).next_sync_round == interval * 2


async def test_nonempty_graph_and_source_restore_after_actual_compaction(
    database, unique_name, monkeypatch
):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from agent_workbench.agent.context_sources import persist_history
    from agent_workbench.agent.middleware.compaction import GuardedSummarizationMiddleware
    from agent_workbench.evidence.artifacts import ArtifactStore

    box = await ready(database, unique_name)
    box.artifacts = ArtifactStore(database.settings)
    source = await persist_history(
        box, [ToolMessage(content="retained source", tool_call_id="src")]
    )
    batch = adapter.validate_python(
        [
            {
                "op": "add_node",
                "graph": "information",
                "temp_id": "e",
                "data": {
                    "label": "local observation",
                    "source_refs": ["artifact:" + source["artifact_id"]],
                },
            },
            {
                "op": "add_node",
                "graph": "attack",
                "temp_id": "s",
                "data": {"summary": "local state", "entity_refs": ["e"]},
            },
            {
                "op": "add_edge",
                "graph": "attack",
                "temp_id": "a",
                "data": {
                    "from_id": "s",
                    "to_id": "s",
                    "action": "local check",
                    "outcome": "negative",
                    "source_refs": ["artifact:" + source["artifact_id"]],
                },
            },
        ]
    )
    async with database.transaction() as db:
        await update_graph(db, box, "seed", 0, batch)
    async with database.session() as db:
        before = await read_graph(db, box.session_id)

    # Replace only the model-call seam; actual archive, message replacement and state validation run.
    async def local_summary(*args, **kwargs):
        return AIMessage(content="local summary; original source remains in archive")

    monkeypatch.setattr(
        "agent_workbench.agent.middleware.compaction.invoke_chat_model", local_summary
    )
    middleware = GuardedSummarizationMiddleware(
        model=FakeListChatModel(responses=["unused"]), trigger=("messages", 4), keep=("messages", 2)
    )
    update = await middleware.abefore_model(
        {
            "messages": [HumanMessage(content=f"old {i}", id=f"u{i}") for i in range(8)],
            "objective": "local fixture",
        },
        SimpleNamespace(context=SimpleNamespace(toolbox=box)),
    )
    assert update is not None
    assert update["work_summary"]["summary_version"] == 1
    archive = update["work_summary"]["source_archive_artifact_id"]
    from agent_workbench.persistence.tables import ArtifactRow

    async with database.session() as db:
        assert await read_graph(db, box.session_id) == before
        row = await db.get(ArtifactRow, source["artifact_id"])
        assert (
            json.loads(box.artifacts.read_bytes(row.relative_key))["messages"][0]["content"]
            == "retained source"
        )
        assert await db.get(ArtifactRow, archive) is not None


async def test_parallel_batch_counts_only_after_all_tool_messages(database, unique_name):
    box = await ready(database, unique_name)
    message = decision("parallel")
    message.tool_calls.append({"id": "other", "name": "fixture_read", "args": {}})
    state = {
        "messages": [
            message,
            ToolMessage(content="first", tool_call_id=message.tool_calls[0]["id"]),
        ]
    }
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box))
    middleware = WorkingContextMiddleware()
    await middleware.abefore_model(cast(Any, state), cast(Any, runtime))
    assert box.working_context["session_memory"]["rounds"] == 0
    state["messages"].append(ToolMessage(content="second", tool_call_id="other"))
    await middleware.abefore_model(cast(Any, state), cast(Any, runtime))
    assert box.working_context["session_memory"]["rounds"] == 1
    await middleware.abefore_model(cast(Any, state), cast(Any, runtime))
    assert box.working_context["session_memory"]["rounds"] == 1


async def test_budget_drops_unpresentable_graph_and_ack_credential(database, unique_name):
    from tests.unit.test_context_budget import profile

    box = await ready(database, unique_name)
    box.graph_sync_snapshot = {"complete": True, "through_seq": 99, "rounds": 5}
    box.working_context = {"session_memory": {"graph": {"label": "x" * 40000}}}
    graph = create_agent(
        ScriptedChatModel(responses=[final_text("自然结束")]),
        tools=[],
        context_schema=RunContext,
        middleware=[ContextBuilderMiddleware(profile(5000, 1000))],
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="local")]}, context=RunContext(toolbox=cast(Any, box))
    )
    assert result["messages"][-1].content == "自然结束"
    assert box.graph_sync_snapshot is None
    view = box.context_manifest["working_context"]["session_memory"]
    assert view["overflow"] is True and "graph" not in view


async def test_information_edge_forward_reference_and_update(database, unique_name):
    box = await ready(database, unique_name)
    batch = adapter.validate_python(
        [
            {
                "op": "add_edge",
                "graph": "information",
                "temp_id": "r",
                "data": {
                    "from_id": "one",
                    "to_id": "two",
                    "relation": "related",
                    "status": "hypothesis",
                },
            },
            {"op": "add_node", "graph": "information", "temp_id": "one", "data": {"label": "one"}},
            {"op": "add_node", "graph": "information", "temp_id": "two", "data": {"label": "two"}},
        ]
    )
    async with database.transaction() as db:
        result = await update_graph(db, box, "forward", 0, batch)
    ids = result["id_mapping"]
    change = adapter.validate_python(
        [
            {
                "op": "update_edge",
                "graph": "information",
                "id": ids["r"],
                "data": {
                    "from_id": ids["one"],
                    "to_id": ids["two"],
                    "relation": "related",
                    "status": "refuted",
                },
            }
        ]
    )
    async with database.transaction() as db:
        await update_graph(db, box, "revise", 1, change)
    async with database.session() as db:
        restored = await read_graph(db, box.session_id)
        assert len(restored["information_graph"]["edges"]) == 1
        assert restored["information_graph"]["edges"][0]["status"] == "refuted"


async def test_typed_endpoints_atomic_feedback(database, unique_name):
    from agent_workbench.contracts.enums import ToolEffectClass
    from agent_workbench.tools.operation import OperationRegistry

    box = await ready(database, unique_name)
    async with database.transaction() as db:
        op = await OperationRegistry(db).prepare(
            run_id=box.run_id,
            session_id=box.session_id,
            tool_call_id="source",
            tool_name="fixture",
            arguments={},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
    batch = [
        {"op": "add_node", "graph": "information", "temp_id": x, "data": {"label": x}}
        for x in ("e1", "e2")
    ] + [
        {
            "op": "add_edge",
            "graph": "information",
            "temp_id": "r",
            "data": {"from_id": "e1", "to_id": "e2", "relation": "related", "status": "observed"},
        },
        *[
            {
                "op": "add_node",
                "graph": "attack",
                "temp_id": s,
                "data": {"summary": s, "entity_refs": [e]},
            }
            for s, e in [("s0", "e1"), ("s1", "e2")]
        ],
        {
            "op": "add_edge",
            "graph": "attack",
            "temp_id": "a",
            "data": {
                "from_id": "s0",
                "to_id": "e2",
                "action": "local check",
                "outcome": "unknown",
                "source_refs": [f"operation:{op.operation_id}"],
            },
        },
    ]
    with pytest.raises(
        ValueError, match=r"operations\[5\].data.to_id=e2.*InformationGraph Entity.*AttackState"
    ):
        async with database.transaction() as db:
            await update_graph(db, box, "bad-types", 0, adapter.validate_python(batch))
    async with database.session() as db:
        assert (await read_graph(db, box.session_id))["information_graph"]["nodes"] == []
    batch[5]["data"]["to_id"] = "s1"
    async with database.transaction() as db:
        await update_graph(db, box, "good-types", 0, adapter.validate_python(batch))
    async with database.session() as db:
        saved = await read_graph(db, box.session_id)
        assert saved["revision"] == 1
        assert len(saved["information_graph"]["edges"]) == len(saved["attack_graph"]["edges"]) == 1


async def test_filtered_bounded_pages_and_durable_late_ack(database, unique_name):
    from agent_workbench.contracts.events import EventType
    from agent_workbench.observability.events import append_event
    from agent_workbench.persistence.tables import SessionGraphReceiptRow

    box = await ready(database, unique_name)
    box.settings.graph_inject_every_round = True
    box.settings.graph_context_chars = 2400
    async with database.transaction() as db:
        for i in range(240):
            await append_event(
                db,
                session_id=box.session_id,
                run_id=box.run_id,
                event_type=EventType.RUN_STATUS,
                payload={"ordinary": i},
            )
        for i in range(40):
            await append_event(
                db,
                session_id=box.session_id,
                run_id=box.run_id,
                event_type=EventType.TOOL_COMPLETED,
                payload={"tool": "fixture", "status": str(i)},
            )
    first = await graph_context(box, None)
    assert first["auxiliary_events"] and first["through_seq"] > 240
    assert first["remaining_events"] > 0
    assert len(json.dumps(first, ensure_ascii=False)) < box.settings.graph_context_chars
    with pytest.raises(ValueError, match="未展示"):
        async with database.transaction() as db:
            await update_graph(db, box, "unseen", 0, [], page_id=first["page_id"])
    await present(box)
    fresh = SimpleNamespace(**vars(box))
    fresh.graph_sync_snapshot = None
    await graph_context(fresh, decision(0))  # A later request cannot erase the durable delivery.
    async with database.transaction() as db:
        ack = await update_graph(db, fresh, "late", 0, [], page_id=first["page_id"])
    assert ack["reviewed_seq"] == first["through_seq"]
    async with database.transaction() as db:
        assert await update_graph(db, fresh, "late", 0, [], page_id=first["page_id"]) == ack
    with pytest.raises(ValueError, match="版本冲突"):
        async with database.transaction() as db:
            await update_graph(db, fresh, "duplicate", 1, [], page_id=first["page_id"])
    other = await ready(database, unique_name + "foreign")
    with pytest.raises(ValueError, match="不属于"):
        async with database.transaction() as db:
            await update_graph(db, other, "foreign", 0, [], page_id=first["page_id"])
    # A presented page with a discontinuous start still cannot jump across history.
    async with database.transaction() as db:
        db.add(
            SessionGraphReceiptRow(
                session_id=box.session_id,
                request_id="fixture-gap",
                digest="fixture",
                receipt={
                    "presented": True,
                    "revision": 1,
                    "from_seq": first["through_seq"] + 1,
                    "through_seq": first["through_seq"] + 9,
                },
            )
        )
    with pytest.raises(ValueError, match="不连续"):
        async with database.transaction() as db:
            await update_graph(db, fresh, "gap", 1, [], page_id="fixture-gap")
    seen = [x["seq"] for x in first["auxiliary_events"]]
    revision = 1
    while True:
        page = await graph_context(fresh, None)
        seen.extend(x["seq"] for x in page["auxiliary_events"])
        await present(fresh)
        async with database.transaction() as db:
            await update_graph(db, fresh, f"page-{revision}", revision, [], page_id=page["page_id"])
        revision += 1
        if not page["remaining_events"]:
            break
        assert revision < 20
    assert len(seen) == len(set(seen)) == 40


async def test_get_graph_refreshes_stale_page_without_scheduling_or_auto_ack(database, unique_name):
    from agent_workbench.contracts.events import EventType
    from agent_workbench.observability.events import append_event
    from agent_workbench.tools.session_graph import get_graph
    from agent_workbench.tools.session_graph import update_graph as update_tool

    box = await ready(database, unique_name)
    box.settings.graph_bounded_review = True
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box))
    async with database.transaction() as db:
        await append_event(
            db,
            session_id=box.session_id,
            run_id=box.run_id,
            event_type=EventType.TOOL_COMPLETED,
            payload={"tool": "fixture", "status": "succeeded"},
        )
    first = json.loads(await cast(Any, get_graph).coroutine(runtime=runtime))
    old_page = first["pending_page"]["page_id"]
    written = json.loads(
        await cast(Any, update_tool).coroutine(
            runtime=runtime, tool_call_id="without-page", expected_revision=0, operations=[]
        )
    )
    assert not written["history_acknowledged"] and written["reviewed_seq"] == 0
    assert "get_graph" in written["review_hint"]
    with pytest.raises(ValueError, match="页面版本冲突"):
        async with database.transaction() as db:
            await update_graph(db, box, "stale-page", 1, [], page_id=old_page)
    current = json.loads(await cast(Any, get_graph).coroutine(runtime=runtime))
    page = current["pending_page"]
    assert current["revision"] == page["revision"] == 1 and page["page_id"] != old_page
    assert page["acknowledgement"]["page_id"] == page["page_id"]
    async with database.session() as db:
        row = await db.get(SessionGraphRow, box.session_id)
        assert row.reviewed_seq == row.completed_rounds == row.missed_syncs == 0
        assert row.next_sync_round == 5
    confirmed = json.loads(
        await cast(Any, update_tool).coroutine(
            runtime=runtime,
            tool_call_id="confirm-new-page",
            expected_revision=1,
            operations=[],
            page_id=page["page_id"],
        )
    )
    assert confirmed["history_acknowledged"] and confirmed["reviewed_seq"] == page["through_seq"]


@pytest.mark.parametrize(
    "case,review_count,revision",
    [
        ("success", 1, 1),
        ("repair", 2, 1),
        ("exhausted", 2, 0),
        ("prose", 2, 0),
        ("format_repair", 2, 1),
        ("disallowed", 1, 0),
    ],
)
async def test_bounded_review_official_agent_checkpoint_and_segment(
    database, unique_name, record_property, case, review_count, revision
):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from sqlalchemy import select

    from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
    from agent_workbench.agent.middleware.response_recovery import ResponseRecoveryMiddleware
    from agent_workbench.agent.middleware.segment import SegmentMiddleware
    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
    from agent_workbench.agent.state import AgentGraphState
    from agent_workbench.domain.session_graph import check_writer
    from agent_workbench.models.invocation import graph_review_scope
    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.persistence.tables import ModelCallRow, SessionGraphReceiptRow
    from agent_workbench.tools.session_graph import GRAPH_TOOLS
    from tests.unit.test_context_budget import profile

    box = await ready(database, unique_name)
    box.settings.graph_bounded_review = True
    box.sdk_retries_disabled = True
    box.provider_id, box.model_name = "xiaomi", "fixture"
    box.bump = lambda _name: None
    unknown_job = None
    if case == "prose":
        from uuid import uuid4

        from agent_workbench.contracts.enums import ToolEffectClass
        from agent_workbench.persistence.repositories import ToolJobRepository
        from agent_workbench.tools.operation import OperationRegistry

        unknown_job = uuid4()
        async with database.transaction() as db:
            source = await OperationRegistry(db).prepare(
                run_id=box.run_id,
                session_id=box.session_id,
                tool_call_id="unknown-fixture",
                tool_name="fixture",
                arguments={},
                effect_class=ToolEffectClass.READ_ONLY,
                policy_revision=1,
                scope_revision=1,
            )
            source.row.status, source.row.job_id = "unknown", unknown_job
            await ToolJobRepository(db).upsert(
                job_id=unknown_job,
                operation_id=source.operation_id,
                executor_id="fixture",
                state="unknown",
                sync_status="uncertain",
            )

    async def control():
        async with database.transaction() as db:
            await check_writer(db, box)

    box.assert_control_current = control
    work = []
    captured = []
    bindings = []
    budgets = []

    @tool
    async def fixture_read() -> str:
        """只读取本地替身数据，不执行外部操作。"""
        work.append(1)
        return "local result"

    bad = [
        {
            "op": "add_edge",
            "graph": "information",
            "temp_id": "bad",
            "data": {
                "from_id": "missing",
                "to_id": "missing",
                "relation": "bad",
                "status": "hypothesis",
            },
        }
    ]
    good = [
        {
            "op": "add_node",
            "graph": "information",
            "temp_id": "e",
            "data": {"label": "local fixture"},
        }
    ]
    responses = [tool_call("fixture_read", {}, f"work-{i}") for i in range(5)]
    if case == "prose":
        responses.extend(
            [final_text("维护说明：当前没有需要修改的图内容"), final_text("维护说明：仍未提交工具")]
        )
    elif case in {"format_repair", "disallowed"}:
        responses.append(
            final_text("维护说明：尚未提交")
            if case == "format_repair"
            else tool_call("fixture_read", {}, "disallowed")
        )
        if case == "format_repair":
            responses.append(
                tool_call("update_graph", {"expected_revision": 0, "operations": good}, "review-2")
            )
    else:
        responses.append(
            tool_call(
                "update_graph",
                {"expected_revision": 0, "operations": good if case == "success" else bad},
                "review-1",
            )
        )
        if case in {"repair", "exhausted"}:
            responses.append(
                tool_call(
                    "update_graph",
                    {"expected_revision": 0, "operations": good if case == "repair" else bad},
                    "review-2",
                )
            )
    responses.append(final_text("最终报告：本地任务自然结束"))

    class ReviewModel(ScriptedChatModel):
        def bind_tools(self, tools, **kwargs):
            if graph_review_scope.get():
                assert kwargs.get("tool_choice") == "required"
            bindings.append((bool(graph_review_scope.get()), {t.name for t in tools}))
            return self

        def _generate(self, messages, *args, **kwargs):
            index = len(self.seen_message_counts)
            captured.append((bool(graph_review_scope.get()), str(messages[0].content)))
            if graph_review_scope.get():
                budgets.append(box.context_manifest["budget"])
                assert box.context_manifest["request_tool_names"] == ["get_graph", "update_graph"]
                assert box.context_manifest["graph_review_contract"]["page_id"] in str(
                    messages[0].content
                )
            for call in self.responses[index].tool_calls:
                if call["name"] == "update_graph":
                    call["args"]["page_id"] = box.working_context["session_memory"]["page_id"]
            return super()._generate(messages, *args, **kwargs)

    model = ReviewModel(responses=responses)
    from agent_workbench.persistence.tables import RunRow, SessionRow

    async with database.session() as db:
        session_row = await db.get(SessionRow, box.session_id)
        run = await db.get(RunRow, box.run_id)
    config = {"configurable": {"thread_id": session_row.thread_id}, "recursion_limit": 100}
    yields = 0
    payload = {
        "messages": [HumanMessage(content="只做本地替身工作")],
        "active_run_id": str(box.run_id),
        "dispatch_control_version": box.control_version,
    }
    for _ in range(14):
        # Reconnect and clear all ephemeral pointers at each official tools/checkpoint boundary.
        box.graph_sync_snapshot = None
        box.graph_review_window_id = None
        async with open_checkpointer_connection(database.settings) as connection:
            graph = create_agent(
                model,
                tools=[fixture_read, *GRAPH_TOOLS],
                state_schema=AgentGraphState,
                context_schema=RunContext,
                middleware=[
                    SegmentMiddleware(3),
                    WorkingContextMiddleware(),
                    ResponseRecoveryMiddleware(),
                    ContextBuilderMiddleware(profile(100000, 1000)),
                    ModelObserverMiddleware(),
                    ToolDispatchMiddleware(),
                ],
                checkpointer=AsyncPostgresSaver(connection),
                interrupt_after=["tools"],
            )
            result = await graph.ainvoke(
                cast(Any, payload), cast(Any, config), context=RunContext(toolbox=cast(Any, box))
            )
            saved = await graph.aget_state(cast(Any, config))
            if unknown_job and not saved.next and not result.get("segment_yield_reason"):
                from agent_workbench.contracts.enums import RunStatus
                from agent_workbench.evidence.artifacts import ArtifactStore
                from agent_workbench.observability.spans import SpanRecorder
                from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow
                from agent_workbench.runtime.runner import AgentRunner

                runner = AgentRunner(database=database, owner="natural")
                for _again in range(2):
                    assert (
                        await runner._resolve_after_segment(
                            cast(Any, SimpleNamespace(graph=graph)),
                            run,
                            session_row,
                            SpanRecorder(),
                        )
                    )[0] == RunStatus.WAITING_TOOL
                async with database.session() as db:
                    final_run = await db.get(RunRow, box.run_id)
                    from uuid import UUID

                    stored = await db.get(
                        ArtifactRow,
                        UUID(final_run.config_snapshot["completion_result_artifact_id"]),
                    )
                    assert "最终报告：本地任务自然结束" in ArtifactStore(
                        database.settings
                    ).read_bytes(stored.relative_key).decode("utf-8")
                    assert (await db.get(ToolJobRow, unknown_job)).state == "unknown"
        if result.get("segment_yield_reason"):
            yields += 1
            payload = {
                "segment_index": int(result.get("segment_index") or 0) + 1,
                "segment_completed_rounds": 0,
                "segment_yield_reason": None,
            }
        elif not saved.next:
            break
        else:
            payload = None
    else:
        pytest.fail("有限核对未能退出")
    assert yields >= 1
    assert result["messages"][-1].content == "最终报告：本地任务自然结束"
    assert len(work) == 5
    assert len(model.seen_message_counts) == 6 + review_count
    assert all(names == {"get_graph", "update_graph"} for reviewing, names in bindings if reviewing)
    assert all("有限图核对请求" in text for reviewing, text in captured if reviewing)
    if case in {"prose", "exhausted"}:
        assert "本窗口核对已结束" in captured[-1][1]
    assert all("维护说明" not in str(m.content) for m in result["messages"])
    async with database.session() as db:
        row = await db.get(SessionGraphRow, box.session_id)
        window = await db.get(
            SessionGraphReceiptRow, {"session_id": box.session_id, "request_id": "review:5"}
        )
        calls = (
            (await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == box.run_id)))
            .scalars()
            .all()
        )
        assert len(calls) == len(model.seen_message_counts)
        assert len([c for c in calls if c.phase == "graph_review"]) == review_count
        assert window.receipt["attempts"] == review_count
        assert window.receipt["status"] == "closed"
        outcomes = [o["outcome"] for o in window.receipt["observations"]]
        assert ("accepted_update" in outcomes) == bool(revision)
        if case in {"prose", "format_repair"}:
            assert "no_tool_call" in outcomes
        if case == "disallowed":
            assert "disallowed_tool" in outcomes
        assert row.completed_rounds == 5 and row.revision == revision
        assert (row.reviewed_seq > 0) == bool(revision)
    record_property("extra_review_requests", review_count)
    record_property("review_request_budgets", budgets)


@pytest.mark.parametrize("control_kind", ["pause", "cancel", "guidance"])
async def test_bounded_review_control_priority_and_spent_quota(database, unique_name, control_kind):
    import asyncio

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from sqlalchemy import select

    from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
    from agent_workbench.agent.middleware.response_recovery import ResponseRecoveryMiddleware
    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
    from agent_workbench.agent.signals import GraphRunAborted, StaleResponseError
    from agent_workbench.agent.state import AgentGraphState
    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.persistence.tables import ModelCallRow, RunRow, SessionGraphReceiptRow
    from agent_workbench.tools.session_graph import GRAPH_TOOLS

    box = await ready(database, unique_name)
    box.settings.graph_bounded_review = True
    box.sdk_retries_disabled = True
    box.provider_id, box.model_name = "xiaomi", "fixture"
    box.bump = lambda _name: None
    work = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def control():
        async with database.session() as db:
            row = await db.get(RunRow, box.run_id)
            if row.status != "running":
                raise GraphRunAborted(row.status)
            if row.control_version != box.control_version:
                raise StaleResponseError(
                    pinned_version=box.control_version, current_version=row.control_version
                )

    box.assert_control_current = control

    @tool
    async def fixture_read() -> str:
        """只返回本地结果。"""
        work.append(1)
        return "fixture"

    class InterruptibleModel(ScriptedChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if len(self.seen_message_counts) == 5:
                entered.set()
                await release.wait()
            return self._generate(messages, stop=stop, **kwargs)

    model = InterruptibleModel(
        responses=[tool_call("fixture_read", {}, f"w{i}") for i in range(5)]
        + [tool_call("update_graph", {"expected_revision": 0, "operations": []}, "late-review")]
    )
    config = cast(
        Any, {"configurable": {"thread_id": f"control-{box.run_id}"}, "recursion_limit": 100}
    )

    def build(model, saver):
        return create_agent(
            model,
            tools=[fixture_read, *GRAPH_TOOLS],
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=saver,
            middleware=[
                WorkingContextMiddleware(),
                ResponseRecoveryMiddleware(),
                ContextBuilderMiddleware(),
                ModelObserverMiddleware(),
                ToolDispatchMiddleware(),
            ],
        )

    async with open_checkpointer_connection(database.settings) as connection:
        graph = build(model, AsyncPostgresSaver(connection))
        pending = asyncio.create_task(
            graph.ainvoke(
                cast(
                    Any,
                    {
                        "messages": [HumanMessage(content="local")],
                        "active_run_id": str(box.run_id),
                        "dispatch_control_version": box.control_version,
                    },
                ),
                config,
                context=RunContext(toolbox=cast(Any, box)),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            async with database.transaction() as db:
                row = await db.get(RunRow, box.run_id)
                row.status = {"pause": "pausing", "cancel": "cancelling", "guidance": "running"}[
                    control_kind
                ]
                row.control_version += 1
            release.set()
            with pytest.raises(
                StaleResponseError if control_kind == "guidance" else GraphRunAborted
            ):
                await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    async with database.session() as db:
        window = await db.get(
            SessionGraphReceiptRow, {"session_id": box.session_id, "request_id": "review:5"}
        )
        assert window.receipt["attempts"] == 1
        assert (await db.get(SessionGraphRow, box.session_id)).revision == 0
    assert len(work) == 5 and len(model.seen_message_counts) == 6
    if control_kind == "cancel":
        return
    # Only this isolated fixture simulates explicit resume/new guidance, never a real Run.
    async with database.transaction() as db:
        row = await db.get(RunRow, box.run_id)
        row.status = "running"
        box.control_version = row.control_version
    box.graph_review_window_id = None
    box.graph_sync_snapshot = None
    resumed = ScriptedChatModel(responses=[final_text("恢复后直接自然结束")])
    async with open_checkpointer_connection(database.settings) as connection:
        graph = build(resumed, AsyncPostgresSaver(connection))
        result = await graph.ainvoke(
            cast(Any, {"dispatch_control_version": box.control_version}),
            config,
            context=RunContext(toolbox=cast(Any, box)),
        )
    assert result["messages"][-1].content == "恢复后直接自然结束"
    assert len(resumed.seen_message_counts) == 1 and len(work) == 5
    async with database.session() as db:
        window = await db.get(
            SessionGraphReceiptRow, {"session_id": box.session_id, "request_id": "review:5"}
        )
        assert window.receipt["attempts"] == 1 and window.receipt["status"] == "closed"
        calls = (
            (
                await db.execute(
                    select(ModelCallRow).where(
                        ModelCallRow.run_id == box.run_id, ModelCallRow.phase == "graph_review"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(calls) == 1
