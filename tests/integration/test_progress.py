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
    ProgressMiddleware,
    next_progress_state,
    semantic_progress_signature,
)
from agent_workbench.agent.signals import NoProgressBlocked
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import CoverageStatus
from agent_workbench.domain.findings import CoverageService, FindingService
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    ProjectRepository,
    SessionRepository,
)
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
    assert with_evidence != baseline

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


async def test_streak_warns_then_pauses_at_clean_model_boundary() -> None:
    first = next_progress_state("", 0, "same")
    assert first["no_progress_streak"] == 0
    repeated = next_progress_state("same", PAUSE_STREAK - 1, "same")
    assert repeated["no_progress_streak"] == PAUSE_STREAK
    changed = next_progress_state("same", PAUSE_STREAK, "new")
    assert changed["no_progress_streak"] == 0
    with pytest.raises(NoProgressBlocked):
        await ProgressMiddleware().abefore_model(repeated, None)  # type: ignore[arg-type]


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
            middleware=[ProgressMiddleware()],
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
        with pytest.raises(NoProgressBlocked):
            await graph.ainvoke(
                blocked,
                config=RunnableConfig(
                    configurable={"thread_id": f"progress-blocked-{uuid4().hex}"}
                ),
                context=context,
            )
