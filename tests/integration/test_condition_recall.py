"""条件变化召回与弱点消费投影（P0 呈现 + P2 召回）。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_workbench.agent.working_context import assemble_working_context
from agent_workbench.contracts.enums import Actionability
from agent_workbench.contracts.weaknesses import WeaknessRelationRef
from agent_workbench.domain.exploration_review import (
    recall_by_condition_change,
    weakness_condition_changes,
)
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.weakness_relations import upsert_links
from agent_workbench.persistence.tables import LeadRow
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def test_new_weakness_enters_context_even_without_any_relation(database, unique_name):
    """没有显式关系的新弱点必须有进入待分析列表的机会，否则无法建立第一条关系。"""
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        created = await FindingService(db).upsert_weakness(
            session_id=session_id,
            title="孤立的弱点",
            description="还没有任何关系",
            provided_conditions=[{"statement": "返回了内部版本号"}],
        )
    async with database.session() as db:
        view = await assemble_working_context(db, session_id, {"objective": "本地目标"})
    consumption = view["weakness_consumption"]
    assert [item["weakness_id"] for item in consumption["changed_weaknesses"]] == [
        str(created.weakness_id)
    ]
    assert consumption["changed_weaknesses"][0]["has_relation"] is False
    assert consumption["pending_connection_review_count"] == 1
    assert str(created.weakness_id) in consumption["pending_connection_review_ids"]
    assert consumption["consumption_commit"]["presented_weakness_ids"] == [
        str(created.weakness_id)
    ]


async def test_recall_prefers_explicit_relations_and_dedupes(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        weakness = await FindingService(db).upsert_weakness(
            session_id=session_id,
            title="提供只读会话",
            provided_conditions=[{"statement": "存在只读账号"}],
        )
        leads = LeadService(db)
        linked = await leads.upsert(
            session_id=session_id,
            hypothesis="只读账号可写",
            next_action="用只读账号 POST",
            identity_refs=["readonly"],
        )
        await upsert_links(
            db,
            session_id=session_id,
            lead_id=linked.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id),
                    role="basis",
                    condition_key=weakness.condition_keys[0],
                    source_weakness_revision=weakness.revision,
                )
            ],
        )
        unrelated = await leads.upsert(
            session_id=session_id, hypothesis="无关线索", next_action="探测首页"
        )
    async with database.session() as db:
        recalled = await recall_by_condition_change(
            db, session_id, new_weakness_ids=[weakness.weakness_id]
        )
    assert [item.lead_id for item in recalled] == [str(linked.id)]
    assert recalled[0].trigger == "new_weakness"
    assert recalled[0].relation_ids, "明确关系必须带出关系 ID"
    assert all(item.lead_id != str(unrelated.id) for item in recalled)

    # 去重键按“对象 + 版本 + 触发原因”，同一变化不会每轮重复召回同一批内容。
    async with database.session() as db:
        again = await recall_by_condition_change(
            db, session_id, new_weakness_ids=[weakness.weakness_id]
        )
    assert [item.dedupe_key for item in again] == [item.dedupe_key for item in recalled]


async def test_identity_change_and_mainline_block_recall_existing_routes(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        leads = LeadService(db)
        identified = await leads.upsert(
            session_id=session_id,
            hypothesis="依赖登录身份",
            next_action="切换身份重放",
            identity_refs=["session-a"],
        )
        other = await leads.upsert(
            session_id=session_id, hypothesis="另一条未完成路线", next_action="探测备份文件"
        )
    async with database.session() as db:
        identity_recall = await recall_by_condition_change(db, session_id, identity_changed=True)
    assert str(identified.id) in {item.lead_id for item in identity_recall}
    assert all(item.trigger == "identity_changed" for item in identity_recall)

    async with database.session() as db:
        blocked_recall = await recall_by_condition_change(db, session_id, mainline_blocked=True)
    assert str(other.id) in {item.lead_id for item in blocked_recall}
    assert {item.trigger for item in blocked_recall} == {"mainline_blocked"}


async def test_refuted_source_marks_downstream_for_review_only(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        service = FindingService(db)
        weakness = await service.upsert_weakness(session_id=session_id, title="可疑条件")
        leads = LeadService(db)
        lead = await leads.upsert(
            session_id=session_id, hypothesis="依赖可疑条件", next_action="重放"
        )
        await upsert_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id),
                    role="basis",
                    source_weakness_revision=weakness.revision,
                )
            ],
        )
        await leads.assess_actionability(
            lead.id, state=str(Actionability.READY), note="条件已具备"
        )
        # 反证会推进弱点版本 -> 下游判断降级为 needs_review，但不判定线索失败。
        await service.upsert_weakness(
            session_id=session_id,
            weakness_id=weakness.weakness_id,
            expected_revision=weakness.revision,
            description="复测后不成立",
        )
        row = await db.get(LeadRow, lead.id)
        assert row is not None
        assert (row.actionability_assessment or {}).get("state") == str(Actionability.NEEDS_REVIEW)
        assert row.status == "new"

    async with database.session() as db:
        recalled = await recall_by_condition_change(
            db, session_id, refuted_weakness_ids=[weakness.weakness_id]
        )
    assert [item.trigger for item in recalled] == ["weakness_refuted"]


async def test_condition_change_projection_reports_added_and_revised(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        created = await FindingService(db).upsert_weakness(
            session_id=session_id, title="变化中的弱点"
        )
    async with database.session() as db:
        added = await weakness_condition_changes(db, session_id, since_revision={})
        baseline = {str(created.weakness_id): 1}
        revised = await weakness_condition_changes(
            db, session_id, since_revision=baseline
        )
    assert added[0]["change"] == "added"
    assert revised == [], "版本没变就不该报出变化"

    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(
            session_id=session_id,
            weakness_id=created.weakness_id,
            expected_revision=1,
            provided_conditions=[{"statement": "新增的条件"}],
        )
    async with database.session() as db:
        changes = await weakness_condition_changes(db, session_id, since_revision=baseline)
    assert changes[0]["change"] == "revised"
    assert changes[0]["revision"] == 2


async def test_working_context_reports_condition_changes_and_review_pending(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        finding = await FindingService(db).upsert_weakness(
            session_id=session_id,
            title="变化弱点",
            provided_conditions=[{"statement": "条件一"}],
        )
    state = {"objective": "本地目标"}
    async with database.session() as db:
        first = await assemble_working_context(db, session_id, state)
    commit = first["next"]["consumption_commit"]
    state.update(
        last_weakness_revisions=commit["weakness_revisions"],
        presented_weakness_ids=commit["presented_weakness_ids"],
    )
    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(
            session_id=session_id,
            weakness_id=finding.weakness_id,
            expected_revision=1,
            provided_conditions=[{"statement": "条件二"}],
        )
    async with database.session() as db:
        second = await assemble_working_context(db, session_id, state)
    consumption = second["weakness_consumption"]
    assert consumption["revised_weakness_ids"] == [str(finding.weakness_id)]
    assert consumption["condition_changes"][0]["change"] == "revised"
    assert consumption["pending_connection_review_count"] == 1
    assert consumption["changed_weaknesses"][0]["already_presented"] is True

    # 记录用途判断后不再是待分析项。
    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(
            session_id=session_id,
            weakness_id=finding.weakness_id,
            expected_revision=2,
            connection_review={"no_connection_note": "暂时没有可推进连接"},
        )
    async with database.session() as db:
        third = await assemble_working_context(
            db,
            session_id,
            {**state, "last_weakness_revisions": second["next"]["consumption_commit"]["weakness_revisions"]},
        )
    assert third["weakness_consumption"]["pending_connection_review_count"] == 0
    assert third["weakness_consumption"]["changed_weaknesses"] == []
    async with database.session() as db:
        rows = (await db.execute(select(LeadRow).where(LeadRow.session_id == session_id))).scalars().all()
    assert rows == []
