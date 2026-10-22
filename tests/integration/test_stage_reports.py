"""阶段快照不冒充完成，原版本可重试且历史内容不受后续领域修改污染。"""

import json
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.finish_gate import FinishGateMiddleware
from agent_workbench.agent.middleware.stage_report import StageReportMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.reports import create_report_snapshot
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import ReportVersionRow
from agent_workbench.runtime.stopping import FinishGate
from agent_workbench.tools.completion import propose_completion
from tests.integration.test_request_budget import context_for
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

pytestmark = pytest.mark.integration


async def test_stage_report_no_confirmed_is_deduplicated_and_failure_retries_original_snapshot(
    database, unique_name, monkeypatch
):
    context = await context_for(database, unique_name)
    store = ArtifactStore(database.settings)
    metadata = {
        "stage_key": "fixed-stage-key",
        "goal_progress": ["检查一项条件"],
        "next_steps": ["复核旧线索"],
    }
    original = store.write_bytes

    def fail(*args: Any, **kwargs: Any):
        raise OSError("模拟渲染存储失败")

    monkeypatch.setattr(store, "write_bytes", fail)
    with pytest.raises(OSError):
        await create_report_snapshot(
            database, store, context.toolbox.session_id, stage_metadata=metadata
        )
    async with database.session() as db:
        first = (
            await db.execute(
                select(ReportVersionRow).where(
                    ReportVersionRow.session_id == context.toolbox.session_id
                )
            )
        ).scalar_one()
        original_snapshot = json.loads(json.dumps(first.captured_snapshot))
        assert first.generation_status == "failed"
    async with database.transaction() as db:
        await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="报告失败后新建的线索"
        )
    monkeypatch.setattr(store, "write_bytes", original)
    result = await create_report_snapshot(
        database, store, context.toolbox.session_id, stage_metadata=metadata
    )
    assert result.version == first.version and result.confirmed_count == 0
    monkeypatch.setattr(store, "write_bytes", fail)
    repeat = await create_report_snapshot(
        database, store, context.toolbox.session_id, stage_metadata=metadata
    )
    assert repeat == result
    async with database.session() as db:
        reports = (
            (
                await db.execute(
                    select(ReportVersionRow).where(
                        ReportVersionRow.session_id == context.toolbox.session_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(reports) == 1 and reports[0].captured_snapshot == original_snapshot
    assert reports[0].generation_status == "completed"


async def test_stage_report_only_triggers_for_domain_turning_point(database, unique_name):
    context = await context_for(database, unique_name)
    middleware = StageReportMiddleware(enabled=True)
    runtime = cast(Any, type("Runtime", (), {"context": context})())
    state: dict[str, Any] = {"plan": []}
    state.update(await middleware.abefore_model(cast(Any, state), runtime) or {})
    assert await middleware.abefore_model(cast(Any, state), runtime) is None
    state["plan"] = [{"step_id": "s1", "status": "done", "description": "小目标完成"}]
    update = await middleware.abefore_model(cast(Any, state), runtime)
    assert update and update["report_retry_key"] is None
    state.update(update)
    assert await middleware.abefore_model(cast(Any, state), runtime) is None
    async with database.session() as db:
        report = (
            await db.execute(
                select(ReportVersionRow).where(
                    ReportVersionRow.session_id == context.toolbox.session_id
                )
            )
        ).scalar_one()
    assert (
        report.generation_status == "completed"
        and report.captured_snapshot["body"]["confirmed"] == []
    )


async def test_optional_leads_do_not_block_finish_but_forged_evidence_does(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        lead = await LeadService(db).upsert(session_id=context.toolbox.session_id,
            hypothesis="保留未验证的假设", next_action="未来继续")
    proposal = {"reason": "goal_answered", "goal_answer": "已取得阶段结果，假设未验证"}
    async with database.session() as db:
        gate = FinishGate(db)
        assert (await gate.evaluate(session_id=context.toolbox.session_id, proposal=proposal)).allowed
        assert not (await gate.evaluate(session_id=context.toolbox.session_id,
            proposal={**proposal, "evidence_refs": ["不存在的证据"]})).allowed
        from agent_workbench.persistence.tables import LeadRow
        row = await db.get(LeadRow, lead.id)
        assert row.status == "new", "结束不能顺便关闭或确认假设"




async def test_repeated_completion_rejections_continue_without_user_or_extra_evaluator(
    database, unique_name
):
    context = await context_for(database, unique_name)
    context.toolbox.spans = SpanRecorder()
    calls = [
        tool_call(
            "propose_completion",
            {
                "reason": "exploration_converged",
                "goal_answer": "未登记覆盖",
                "evidence_refs": ["不存在的证据"],
            },
            f"proposal-{index}",
        )
        for index in range(5)
    ]
    calls += [
        tool_call(
            "propose_completion",
            {
                "reason": "goal_answered",
                "goal_answer": "明确回答边界",
            },
            "final-proposal",
        ),
        final_text("不应调用"),
    ]
    model = ScriptedChatModel(responses=calls)
    graph = create_agent(
        model=model,
        tools=[propose_completion],
        middleware=[FinishGateMiddleware()],
        state_schema=AgentGraphState,
        context_schema=RunContext,
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="本地提议协议测试")]}),
        {"recursion_limit": 100},
        context=context,
    )
    assert result["completion_proposal"]["reason"] == "goal_answered"
    assert result["finish_gate_attempts"] == 5
    assert len(model.seen_message_counts) == 6
