"""来源保留、全量分页、只读召回和官方 checkpoint 适配回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent.context_sources import persist_history
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.agent.state_migration import ensure_current_checkpoint
from agent_workbench.agent.working_context import assemble_working_context
from agent_workbench.domain.leads import LeadService
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import FindingRow, LeadRow, ObservationRow, ToolOperationRow
from agent_workbench.tools.history import query_history
from agent_workbench.tools.task import propose_finding, record_lead, record_observation
from tests.integration.test_request_budget import context_for
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", ["record_observation", "propose_finding"])
async def test_domain_commit_before_checkpoint_replays_one_receipt_and_explicit_retest_is_new(
    database, unique_name, name
):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=name + "-call",
        store=None,
    )
    selected = record_observation if name == "record_observation" else propose_finding
    args = (
        {"summary": "一条本地观察"}
        if name == "record_observation"
        else {"title": "本地候选", "description": "只登记候选，不宣称已验证"}
    )
    fn = cast(Any, selected).coroutine
    original = await fn(runtime, **args, tool_call_id=name + "-call")
    assert await fn(runtime, **args, tool_call_id=name + "-call") == original
    model = ObservationRow if name == "record_observation" else FindingRow
    async with database.session() as db:
        rows = (
            (await db.execute(select(model).where(model.session_id == context.toolbox.session_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    await fn(runtime, **args, tool_call_id=name + "-explicit-retest")
    async with database.session() as db:
        rows = (
            (await db.execute(select(model).where(model.session_id == context.toolbox.session_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 2


async def test_rotation_and_history_cover_more_than_two_hundred_without_scores(
    database, unique_name
):
    context = await context_for(database, unique_name)
    start = datetime(2020, 1, 1, tzinfo=UTC)
    expected = []
    async with database.transaction() as db:
        for index in range(207):
            row = await LeadService(db).upsert(
                session_id=context.toolbox.session_id, hypothesis=f"中文旧线索 {index}"
            )
            row.created_at = start + timedelta(seconds=index)
            expected.append(str(row.id))
    state: dict[str, Any] = {
        "objective": "无匹配目标",
        "active_lead_ids": [],
        "completed_decision_rounds": 0,
    }
    rotated = []
    for index in range(207):
        state["completed_decision_rounds"] = index * 8
        async with database.session() as db:
            view = await assemble_working_context(db, context.toolbox.session_id, state)
        selected = view["judgment"]["leads"]
        old = next(item for item in selected if item["recall_reason"] == "stable_old_lead_rotation")
        rotated.append(old["lead_id"])
        state.update(
            review_cursor=view["next"]["cursor_after_selection"], last_review_turn=index * 8
        )
        assert len(selected) <= 4
    assert rotated == expected
    seen = []
    cursor = None
    while True:
        page = await query_history(
            context.toolbox, {}, query="中文旧线索", kind="lead", cursor=cursor
        )
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert set(seen) == set(expected) and len(seen) == 207
    with pytest.raises(ValueError, match="不一致"):
        first = await query_history(context.toolbox, {}, kind="lead")
        await query_history(
            context.toolbox, {}, query="变更查询", kind="lead", cursor=first["next_cursor"]
        )


async def test_merge_preserves_sources_status_and_rejects_cycles_and_other_sessions(
    database, unique_name
):
    context = await context_for(database, unique_name)
    other = await context_for(database, unique_name + "-other")
    async with database.transaction() as db:
        svc = LeadService(db)
        a = await svc.upsert(
            session_id=context.toolbox.session_id, hypothesis="源", evidence_refs=["source"]
        )
        b = await svc.upsert(
            session_id=context.toolbox.session_id, hypothesis="目标", evidence_refs=["target"]
        )
        c = await svc.upsert(session_id=other.toolbox.session_id, hypothesis="其他会话")
        await svc.merge(context.toolbox.session_id, a.id, b.id)
        assert a.status == b.status == "new"
        assert a.evidence_refs == ["source"] and b.evidence_refs == ["target", "source"]
        revision = b.revision
        await svc.merge(context.toolbox.session_id, a.id, b.id)
        assert b.revision == revision
        with pytest.raises(ValueError, match="循环"):
            await svc.merge(context.toolbox.session_id, b.id, a.id)
        with pytest.raises(ValueError, match="跨会话"):
            await svc.merge(context.toolbox.session_id, b.id, c.id)


async def test_record_lead_replay_returns_original_receipt_without_new_rows(database, unique_name):
    context = await context_for(database, unique_name)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="lead-call",
        store=None,
    )
    fn = cast(Any, record_lead).coroutine
    first = json.loads(await fn(runtime, "一条假设", "lead-call", next_action="验证条件"))
    repeated = json.loads(await fn(runtime, "一条假设", "lead-call", next_action="验证条件"))
    assert repeated == first
    async with database.session() as db:
        leads = (
            (
                await db.execute(
                    select(LeadRow).where(LeadRow.session_id == context.toolbox.session_id)
                )
            )
            .scalars()
            .all()
        )
        operations = (
            (
                await db.execute(
                    select(ToolOperationRow).where(
                        ToolOperationRow.run_id == context.toolbox.run_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(leads) == len(operations) == 1
    assert leads[0].priority == 0
    with pytest.raises(Exception, match=r"参数.*不一致"):
        await fn(runtime, "不同假设", "lead-call")


async def test_history_visible_archive_excludes_hidden_protocol_and_other_session(
    database, unique_name
):
    context = await context_for(database, unique_name)
    other = await context_for(database, unique_name + "-other")
    context.toolbox.artifacts = ArtifactStore(database.settings)
    await persist_history(
        context.toolbox,
        [
            AIMessage(
                content="中文可见来源",
                id="original-id",
                additional_kwargs={"reasoning_content": "不可检索隐藏正文"},
            )
        ],
    )
    await persist_history(other.toolbox, [HumanMessage(content="其他会话的中文可见来源")])
    page = await query_history(context.toolbox, {}, query="中文可见", kind="conversation")
    assert len(page["items"]) == 1 and page["items"][0]["source"]["message_ids"] == ["original-id"]
    assert (await query_history(context.toolbox, {}, query="隐藏正文"))["items"] == []


@pytest.mark.parametrize("pending_tools", [False, True])
async def test_official_v1_migration_keeps_messages_plan_sources_and_pending_nodes(
    database, settings, unique_name, pending_tools
):
    context = await context_for(database, unique_name)
    submitted = []

    @tool
    def local_submit(key: str) -> str:
        """只记录本地提交。"""
        submitted.append(key)
        return key

    model = ScriptedChatModel(
        responses=[tool_call("local_submit", {"key": "once"}, "call-once"), final_text("完成")]
    )
    config = {"configurable": {"thread_id": str(uuid4())}}
    async with open_checkpointer_connection(settings) as connection:
        graph = create_agent(
            model=model,
            tools=[local_submit],
            state_schema=AgentGraphState,
            checkpointer=AsyncPostgresSaver(connection),
            interrupt_before=["tools"] if pending_tools else None,
        )
        await graph.ainvoke(
            cast(
                Any,
                {
                    "messages": [HumanMessage(content="原目标", id="user-original")],
                    "state_schema_version": 1,
                    "plan": [{"step_id": "s1", "description": "保留", "status": "pending"}],
                    "work_summary": {
                        "source_archive_artifact_id": "immutable-source",
                        "summary_version": 1,
                    },
                },
            ),
            cast(Any, config),
        )
        old = await graph.aget_state(cast(Any, config))
        snapshot = await ensure_current_checkpoint(
            graph, config, old, database, context.toolbox.session_id
        )
        assert snapshot.values["state_schema_version"] == 5
        assert snapshot.next == old.next
        migrated_plan = snapshot.values["plan"]
        assert [step["step_id"] for step in migrated_plan] == [
            step["step_id"] for step in old.values["plan"]
        ]
        assert [step["description"] for step in migrated_plan] == [
            step["description"] for step in old.values["plan"]
        ]
        assert [step["status"] for step in migrated_plan] == [
            step["status"] for step in old.values["plan"]
        ]
        assert snapshot.values["work_summary"] == old.values["work_summary"]
        assert [m.model_dump() for m in snapshot.values["messages"]] == [
            m.model_dump() for m in old.values["messages"]
        ]
        again = await ensure_current_checkpoint(
            graph, config, snapshot, database, context.toolbox.session_id
        )
        assert again.config == snapshot.config
        if pending_tools:
            await graph.ainvoke(None, cast(Any, config))
        assert submitted == ["once"]
        final = await graph.aget_state(cast(Any, config))
        calls = [m for m in final.values["messages"] if isinstance(m, ToolMessage)]
        assert len(calls) == 1
