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
    middleware = StageReportMiddleware()
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


async def test_completion_uses_complete_review_inventory_instead_of_score(database, unique_name):
    """结束复查集合由程序枚举：未申报的线索也拦得住，处置标签不再豁免。"""
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        lead = await LeadService(db).upsert(
            session_id=context.toolbox.session_id,
            hypothesis="相关未决事项",
            impact=5,
            evidence_strength=5,
            feasibility=5,
            information_gain=5,
        )
    proposal = {
        "reason": "goal_answered",
        "goal_answer": "回答当前问题",
        "stop_rationale": "实际证据足够",
    }
    async with database.session() as db:
        gate = FinishGate(db)
        # 即使提议里**没有**申报任何相关线索，未完成的线索仍会被找到。
        decision = await gate.evaluate(session_id=context.toolbox.session_id, proposal=proposal)
        assert not decision.allowed
        page = decision.blocking_objects.get("review_page") or []
        assert any(str(lead.id) in item for item in page), page
        # `outside_goal` + 理由不再是放行凭据。
        explained = {
            **proposal,
            "relevant_lead_ids": [str(lead.id)],
            "coverage_summary": {
                "unresolved_items": [
                    {
                        "lead_id": str(lead.id),
                        "disposition": "outside_goal",
                        "reason": "该问题超出用户本阶段目标，保留来源供后续继续",
                    }
                ]
            },
        }
        assert not (
            await gate.evaluate(session_id=context.toolbox.session_id, proposal=explained)
        ).allowed
        assert not (
            await gate.evaluate(
                session_id=context.toolbox.session_id,
                proposal={**proposal, "evidence_refs": ["不存在的证据"]},
            )
        ).allowed
    # 写明缺失条件后：剩余有效方向确实需要外部条件 -> 等待用户，且保留未决项。
    async with database.transaction() as db:
        await LeadService(db).update_conditions(
            lead.id,
            [{"description": "需要一个已登录账号", "satisfied": False}],
        )
    async with database.session() as db:
        waiting = await FinishGate(db).evaluate(
            session_id=context.toolbox.session_id,
            proposal={**proposal, "missing_conditions": ["测试账号"]},
        )
    assert waiting.decision == "wait_user"
    assert waiting.blocking_objects.get("waiting_items")
    # 没有外部条件说明时不能假装收敛：仍然是可推进/缺条件的未决项。
    async with database.session() as db:
        assert not (
            await FinishGate(db).evaluate(session_id=context.toolbox.session_id, proposal=proposal)
        ).allowed


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
                "stop_rationale": "需要修正",
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
                "stop_rationale": "已解释限制",
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
