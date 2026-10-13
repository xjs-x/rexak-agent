"""重复观察和状态修订不会伪装成探索进展。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.progress import (
    PAUSE_STREAK,
    SIMILAR_RESULT_FEEDBACK,
    SIMILAR_RESULT_STREAK,
    ProgressMiddleware,
    activity_signature,
    next_progress_state,
    next_similarity_state,
    semantic_progress_signature,
    similar_result_signature,
)
from agent_workbench.agent.middleware.segment import SegmentMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import CoverageStatus
from agent_workbench.domain.findings import CoverageService, FindingService
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    ProjectRepository,
    SessionRepository,
    ToolOperationRepository,
)
from tests.integration.test_request_budget import context_for
from tests.support.scripted_model import ScriptedChatModel, final_text

pytestmark = pytest.mark.integration


async def test_signature_ignores_duplicate_evidence_and_unchanged_coverage(
    database,
    unique_name,
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="progress-test",
            name=unique_name,
            target_description="fixture",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="progress",
            scope_snapshot={},
            policy_snapshot={},
        )
        session_id = session.id
    async with database.session() as db:
        baseline = await semantic_progress_signature(db, session_id)

    async with database.transaction() as db:
        await ArtifactRepository(db).create(
            sha256="a" * 64,
            byte_size=1,
            media_type="text/plain",
            relative_key="aa/test",
            session_id=session_id,
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
            captured_at=datetime.now(UTC),
        )
    async with database.session() as db:
        with_evidence = await semantic_progress_signature(db, session_id)
    assert with_evidence == baseline, "仅工件文件变化不能证明测试知识变化"

    async with database.transaction() as db:
        await ArtifactRepository(db).create(
            sha256="a" * 64,
            byte_size=1,
            media_type="text/plain",
            relative_key="aa/duplicate",
            session_id=session_id,
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
            captured_at=datetime.now(UTC),
        )
        await FindingService(db).propose(
            session_id=session_id,
            title="仅候选",
            description="没有验证",
        )
    async with database.session() as db:
        repeated = await semantic_progress_signature(db, session_id)
    assert repeated == with_evidence

    async with database.transaction() as db:
        service = CoverageService(db)
        await service.record(
            session_id=session_id,
            check_category="auth",
            status=CoverageStatus.CHECKED_NEGATIVE,
            entry_label="/login",
            reason="匿名访问 401",
        )
    async with database.session() as db:
        negative = await semantic_progress_signature(db, session_id)
    assert negative != repeated, "明确记录的阴性测试也是有效进展"

    async with database.transaction() as db:
        await CoverageService(db).record(
            session_id=session_id,
            check_category="auth",
            status=CoverageStatus.CHECKED_NEGATIVE,
            entry_label="/login",
            reason="重复记录同一个 401",
        )
    async with database.session() as db:
        duplicate_negative = await semantic_progress_signature(db, session_id)
    assert duplicate_negative == negative


async def test_streak_does_not_pause_at_clean_model_boundary() -> None:
    first = next_progress_state("", 0, "same")
    assert first["no_progress_streak"] == 0
    repeated = next_progress_state("same", PAUSE_STREAK - 1, "same")
    assert repeated["no_progress_streak"] == PAUSE_STREAK
    changed = next_progress_state("same", PAUSE_STREAK, "new")
    assert changed["no_progress_streak"] == 0
    assert await ProgressMiddleware().abefore_model(cast(Any, repeated), cast(Any, None)) is None


async def test_progress_middleware_persists_in_langgraph_checkpoint(
    database,
    settings,
    unique_name,
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="progress-graph",
            name=unique_name,
            target_description="fixture",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="graph",
            scope_snapshot={},
            policy_snapshot={},
        )
        session_id = session.id
    model = ScriptedChatModel(responses=[final_text("继续分析")])
    context = RunContext(toolbox=cast(Any, SimpleNamespace(db=database, session_id=session_id)))
    config: RunnableConfig = {"configurable": {"thread_id": f"progress-{uuid4().hex}"}}
    async with open_checkpointer_connection(settings) as connection:
        graph = create_agent(
            model=model,
            tools=[],
            middleware=[SegmentMiddleware(4), ProgressMiddleware()],
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=AsyncPostgresSaver(connection),
        )
        first: Any = {"messages": [HumanMessage(content="第一次分析")]}
        state1 = await graph.ainvoke(first, config=config, context=context)
        assert state1["no_progress_streak"] == 0
        second: Any = {"messages": [HumanMessage(content="继续")], "no_progress_streak": 0}
        state2 = await graph.ainvoke(second, config=config, context=context)
        assert state2["no_progress_streak"] == 1
        snapshot = await graph.aget_state(config)
        assert snapshot.values["last_progress_signature"] == state2["last_progress_signature"]
        blocked: Any = {
            "messages": [HumanMessage(content="重复循环")],
            "no_progress_streak": PAUSE_STREAK,
            "last_progress_signature": state2["last_progress_signature"],
        }
        repeated_state = await graph.ainvoke(
            blocked,
            config=RunnableConfig(configurable={"thread_id": f"progress-blocked-{uuid4().hex}"}),
            context=context,
        )
        assert repeated_state["messages"][-1].content == "继续分析"


async def _operation(database, context, *, digest: str, tool_call_id: str, status: str):
    async with database.transaction() as db:
        row, _created = await ToolOperationRepository(db).prepare(
            operation_id=uuid4(),
            run_id=context.toolbox.run_id,
            session_id=context.toolbox.session_id,
            tool_call_id=tool_call_id,
            tool_name="http_request",
            request_digest=digest,
            effect_class="read_only",
            policy_revision=1,
            scope_revision=1,
            dispatch_control_version=1,
        )
        row.status = status
    return row


async def test_request_parameters_do_not_change_knowledge_signature(database, unique_name) -> None:
    context = await context_for(database, unique_name)
    async with database.session() as db:
        baseline = await semantic_progress_signature(db, context.toolbox.session_id)
        baseline_activity = await activity_signature(db, context.toolbox.session_id)

    await _operation(database, context, digest="a" * 64, tool_call_id="call-1", status="succeeded")
    async with database.session() as db:
        after_first = await semantic_progress_signature(db, context.toolbox.session_id)
        activity_first = await activity_signature(db, context.toolbox.session_id)
    assert after_first == baseline, "换请求参数不等于获得新知识"
    assert activity_first != baseline_activity, "执行活动是独立信号，不伪装成知识增量"

    await _operation(database, context, digest="b" * 64, tool_call_id="call-2", status="succeeded")
    async with database.session() as db:
        after_second = await semantic_progress_signature(db, context.toolbox.session_id)
    assert after_second == baseline


async def test_similar_result_signature_ignores_parameters_but_tracks_focus(
    database, unique_name
) -> None:
    context = await context_for(database, unique_name)
    await _operation(database, context, digest="c" * 64, tool_call_id="call-3", status="succeeded")
    async with database.session() as db:
        first = await similar_result_signature(db, context.toolbox.session_id, "s1")
        same = await similar_result_signature(db, context.toolbox.session_id, "s1")
        other_focus = await similar_result_signature(db, context.toolbox.session_id, "s2")
    assert first == same, "同一焦点下参数变化不改变相似签名"
    assert first != other_focus, "切换焦点必须重置相似计数"
    a = next_similarity_state("", 0, first)
    b = next_similarity_state(a["last_similar_signature"], a["similar_result_streak"], same)
    assert a["similar_result_streak"] == 0 and b["similar_result_streak"] == 1
    reset = next_similarity_state(
        b["last_similar_signature"], b["similar_result_streak"], other_focus
    )
    assert reset["similar_result_streak"] == 0


async def test_similarity_reminder_is_a_soft_fixed_sentence_with_cooldown(
    database, unique_name
) -> None:
    context = await context_for(database, unique_name)
    await _operation(database, context, digest="d" * 64, tool_call_id="call-4", status="succeeded")
    async with database.session() as db:
        signature = await similar_result_signature(db, context.toolbox.session_id, None)
    middleware = ProgressMiddleware()
    state: dict[str, Any] = {
        "completed_decision_rounds": 10,
        "last_progress_signature": "",
        "no_progress_streak": 0,
        "last_similar_signature": signature,
        "similar_result_streak": SIMILAR_RESULT_STREAK - 1,
    }
    update = await middleware.abefore_model(
        cast(Any, state), cast(Any, SimpleNamespace(context=context))
    )
    assert update is not None
    assert update["progress_feedback"] == SIMILAR_RESULT_FEEDBACK
    assert update["similar_result_streak"] == SIMILAR_RESULT_STREAK
    # 冷却期内不重复；提醒只影响节奏，不阻止请求，也不自动结束任务。
    state.update(update)
    state["completed_decision_rounds"] = 11
    state["last_progress_turn"] = -1
    repeated = await middleware.abefore_model(
        cast(Any, state), cast(Any, SimpleNamespace(context=context))
    )
    assert repeated is None or repeated.get("progress_feedback") is None
