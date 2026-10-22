"""退役结束协议不再产生审查耗时或反馈。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_workbench.agent.middleware.finish_gate import FinishGateMiddleware
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import RunRow, SessionRow, SpanRow
from agent_workbench.runtime.runner import AgentRunner

pytestmark = pytest.mark.integration


async def test_retired_finish_gate_emits_no_review_spans(database, unique_name) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="finish-span-test",
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="结束判定耗时",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = RunRow(
            id=uuid4(),
            session_id=session.id,
            status="running",
            objective="完成测试",
            provider_id="test",
            model_name="scripted",
        )
        db.add(run)
        await db.flush()
        session_id, run_id, thread_id = session.id, run.id, session.thread_id

    proposal = {
        "reason": "goal_answered",
        "goal_answer": "已完成本地目标检查",
        "stop_rationale": "目标已回答",
    }
    spans = SpanRecorder()
    toolbox = SimpleNamespace(db=database, session_id=session_id, run_id=run_id, spans=spans)
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=toolbox))
    await FinishGateMiddleware().aafter_model(
        cast(Any, {"completion_proposal": proposal, "messages": []}), cast(Any, runtime)
    )

    # 历史 proposal 不能再触发审查、反馈或完成。
    async def get_state(_config: Any) -> Any:
        return SimpleNamespace(values={"completion_proposal": proposal}, next=())

    runner = AgentRunner(database=database)
    graph_runtime = SimpleNamespace(graph=SimpleNamespace(aget_state=get_state))
    run_ref = RunRow(id=run_id, session_id=session_id)
    session_ref = SessionRow(id=session_id, thread_id=thread_id)
    status, reason = await runner._resolve_after_segment(cast(Any, graph_runtime), run_ref, session_ref, spans)
    assert status is reason is None
    assert spans.pending == 0

    async with database.session() as db:
        rows = list(
            (
                await db.execute(
                    select(SpanRow).where(SpanRow.run_id == run_id).order_by(SpanRow.started_at)
                )
            ).scalars()
        )
    assert rows == []
