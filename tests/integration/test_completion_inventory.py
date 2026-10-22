"""结束复查集合：程序枚举、分页、过期与等待选择。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import Actionability, LeadStatus
from agent_workbench.contracts.weaknesses import WeaknessRelationRef
from agent_workbench.domain.exploration_review import build_review_inventory
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.weakness_relations import upsert_links
from agent_workbench.persistence.tables import LeadRow
from agent_workbench.runtime.stopping import FinishGate
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration

PROPOSAL = {
    "reason": "goal_answered",
    "goal_answer": "当前问题已回答",
    "stop_rationale": "证据足够",
}


async def _inventory(database, session_id, plan=None, cursor=None):
    """历史复查数据仍可查询，但已退出最小执行和结束路径。"""
    async with database.session() as db:
        return await build_review_inventory(db, session_id, plan=plan, cursor=cursor)


async def test_optional_leads_remain_in_history_without_blocking_completion(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        lead = await leads.upsert(session_id=context.toolbox.session_id, hypothesis="可选假设")
    inventory = await _inventory(database, context.toolbox.session_id)
    assert inventory.unreviewed_count == 1
    assert any(str(lead.id) in item.object_id for item in inventory.items)
    async with database.transaction() as db:
        leads = LeadService(db)
        await leads.upsert(
            session_id=context.toolbox.session_id,
            hypothesis=lead.hypothesis,
            next_action="未来再调查",
            lead_id=lead.id,
        )
        await leads.set_status(
            lead.id, LeadStatus.DEFERRED, reason="排在后面", resume_condition="后续继续"
        )
        await leads.assess_actionability(lead.id, state=str(Actionability.READY), note="下一步明确")
    inventory = await _inventory(database, context.toolbox.session_id)
    assert inventory.actionable_count == 1
    async with database.session() as db:
        assert (
            await FinishGate(db).evaluate(session_id=context.toolbox.session_id, proposal=PROPOSAL)
        ).allowed


async def test_historical_blocked_without_basis_is_still_unreviewed(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        lead = await leads.upsert(session_id=context.toolbox.session_id, hypothesis="历史受阻")
        await leads.set_status(
            lead.id, LeadStatus.BLOCKED, reason="未提供条件", resume_condition="以后"
        )
    assert (await _inventory(database, context.toolbox.session_id)).unreviewed_count == 1


async def test_historical_inventory_keeps_actionable_and_waiting_routes(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        blocked = await leads.upsert(session_id=context.toolbox.session_id, hypothesis="需要账号")
        await leads.update_conditions(
            blocked.id, [{"description": "有效账号", "satisfied": False, "external": True}]
        )
        await leads.assess_actionability(
            blocked.id, state=str(Actionability.NEEDS_CONDITION), note="缺账号"
        )
        await leads.upsert(
            session_id=context.toolbox.session_id, hypothesis="匿名入口", next_action="后续调查"
        )
    inventory = await _inventory(database, context.toolbox.session_id)
    assert inventory.actionable_count == inventory.waiting_count == 1


async def test_optional_waiting_lead_does_not_force_wait_user(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        lead = await leads.upsert(session_id=context.toolbox.session_id, hypothesis="需要账号")
        await leads.update_conditions(
            lead.id, [{"description": "有效账号", "satisfied": False, "external": True}]
        )
        await leads.assess_actionability(
            lead.id, state=str(Actionability.NEEDS_CONDITION), note="缺账号"
        )
    assert (await _inventory(database, context.toolbox.session_id)).waiting_count == 1
    async with database.session() as db:
        assert (
            await FinishGate(db).evaluate(session_id=context.toolbox.session_id, proposal=PROPOSAL)
        ).allowed


async def test_review_inventory_pages_beyond_two_hundred_leads(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        leads = LeadService(db)
        for index in range(205):
            await leads.upsert(session_id=session_id, hypothesis=f"批量线索 {index}")
    first = await _inventory(database, session_id, plan=[])
    assert first.unreviewed_count >= 205, "完整集合不能被 .limit(200) 截断"
    # 分页：游标推进后仍能看到后面的对象，且最后一页回到开头继续核对全量。
    page = first.items
    assert len(page) <= 5
    second = await _inventory(database, session_id, plan=[], cursor=first.next_cursor)
    assert second.items != page
    assert second.unreviewed_count == first.unreviewed_count


async def test_new_weakness_during_review_invalidates_a_clean_snapshot(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(session_id=session_id, title="第一个弱点")
    decision = await _inventory(database, session_id)
    assert decision.unreviewed_count == 1
    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(session_id=session_id, title="复查期间新增的弱点")
    refreshed = await _inventory(database, session_id)
    assert refreshed.unreviewed_count == 2, "放行前必须重新核对全体对象"


async def test_stale_actionability_after_source_weakness_change(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        findings = FindingService(db)
        weakness = await findings.upsert_weakness(
            session_id=session_id,
            title="来源条件",
            provided_conditions=[{"statement": "存在可写入口"}],
        )
        leads = LeadService(db)
        lead = await leads.upsert(
            session_id=session_id, hypothesis="沿条件推进", next_action="重放写入请求"
        )
        # 建立真实关系（不只是用途判断里的引用），该线索才真的依赖这个条件；
        # 判断必须在关系建立**之后**写入，否则依据版本立刻就不同了。
        await upsert_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id),
                    role="basis",
                    condition_key=weakness.condition_keys[0],
                    source_weakness_revision=weakness.revision,
                )
            ],
        )
        await leads.assess_actionability(lead.id, state=str(Actionability.READY), note="条件已具备")
        # 该弱点已完成用途判断，因此复查集合里不应再有未分析弱点。
        await findings.upsert_weakness(
            session_id=session_id,
            weakness_id=weakness.weakness_id,
            expected_revision=weakness.revision,
            connection_review={"linked_lead_ids": [str(lead.id)]},
        )
    clean = await _inventory(database, session_id)
    assert clean.unreviewed_count == 0
    assert clean.actionable_count >= 1

    # 来源弱点条件修订后，原判断过期 -> needs_review（不是继续声称可推进）。
    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(
            session_id=session_id,
            weakness_id=weakness.weakness_id,
            expected_revision=weakness.revision,
            provided_conditions=[{"statement": "可写入口需要管理员会话"}],
        )
    stale = await _inventory(database, session_id)
    assert stale.unreviewed_count >= 1
    async with database.session() as db:
        row = (await db.execute(select(LeadRow).where(LeadRow.id == lead.id))).scalar_one()
    assert (row.actionability_assessment or {}).get("state") == str(Actionability.NEEDS_REVIEW)


async def test_merged_lead_is_reviewed_as_one_object(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        leads = LeadService(db)
        source = await leads.upsert(session_id=session_id, hypothesis="重复假设（来源）")
        target = await leads.upsert(session_id=session_id, hypothesis="重复假设（主）")
        await leads.merge(session_id, source.id, target.id)
    decision = await _inventory(database, session_id)
    assert decision.unreviewed_count == 1, "合并后只复查主对象，不重复算两条任务"
    assert any(str(target.id) in item.object_id for item in decision.items)
    assert not any(str(source.id) in item.object_id for item in decision.items)
