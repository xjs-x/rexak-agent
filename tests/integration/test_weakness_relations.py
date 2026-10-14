"""弱点实体、条件与线索—弱点关系的数据库行为。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import (
    Actionability,
    ErrorCode,
    LeadWeaknessRelationState,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.weaknesses import WeaknessRelationRef
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.weakness_relations import (
    leads_affected_by_weakness,
    links_for_lead,
    refute_links,
    upsert_links,
)
from agent_workbench.persistence.tables import LeadWeaknessLinkRow
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def test_weakness_creation_assigns_stable_condition_keys(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        service = FindingService(db)
        first = await service.upsert_weakness(
            session_id=session_id,
            title="错误页回显内部路径",
            description="访问不存在路径返回包含绝对路径的堆栈",
            weakness_kind="enabling_condition",
            applicability={"limitations": ["仅在 debug 模式"]},
            provided_conditions=[
                {"statement": "错误页包含  绝对路径", "evidence_refs": []},
                {"key": "explicit", "statement": "响应头暴露版本号"},
            ],
        )
        assert first.created is True and first.revision == 1
        assert len(first.condition_keys) == 2
        row = await service.get(first.weakness_id)
        assert row is not None and row.status == "candidate"
        assert row.weakness_kind == "enabling_condition"
        # 同内容重复写入：稳定 key 命中同一条条件，不增加知识进展。
        revision_before = int(row.revision)
        repeat = await service.upsert_weakness(
            session_id=session_id,
            weakness_id=row.id,
            expected_revision=revision_before,
            provided_conditions=[{"statement": "错误页包含 绝对路径"}],
        )
        assert repeat.revision == revision_before
        assert repeat.content_changed is False
        assert repeat.added_condition_keys == []
        assert len(repeat.condition_keys) == 2, "已有条件不会被重复写入覆盖掉"


async def test_conflicting_and_stale_writes_are_rejected(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        service = FindingService(db)
        created = await service.upsert_weakness(
            session_id=session_id, title="候选弱点", description="初次记录"
        )
        row = await service.get(created.weakness_id)
        assert row is not None
        revision_before = int(row.revision)
        bumped = await service.upsert_weakness(
            session_id=session_id,
            weakness_id=row.id,
            expected_revision=revision_before,
            provided_conditions=[{"statement": "多了条件 A"}],
        )
        assert bumped.revision == revision_before + 1
        with pytest.raises(ApiError) as conflict:
            await service.upsert_weakness(
                session_id=session_id,
                weakness_id=row.id,
                expected_revision=revision_before,
                description="用过期的版本覆盖",
            )
        assert conflict.value.code is ErrorCode.CONFLICT


async def test_confirmed_weakness_core_facts_are_not_rewritten_in_place(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    from agent_workbench.contracts.enums import FindingStatus
    from agent_workbench.domain.findings import VerificationService

    async with database.transaction() as db:
        service = FindingService(db)
        created = await service.upsert_weakness(
            session_id=session_id, title="已确认的缺陷", description="原始事实"
        )
        await VerificationService(db).record(
            finding_id=created.weakness_id,
            method_version="fixture-v1",
            conclusion=FindingStatus.CONFIRMED,
            input_evidence_refs=[],
            performed_by="code",
        )
        with pytest.raises(ApiError) as blocked:
            await service.upsert_weakness(
                session_id=session_id,
                weakness_id=created.weakness_id,
                description="悄悄扩大适用范围",
            )
        assert blocked.value.code is ErrorCode.CONFLICT
        # 但**追加条件**是允许的：新条件自己带依据，不继承旧的 confirmed。
        appended = await service.upsert_weakness(
            session_id=session_id,
            weakness_id=created.weakness_id,
            provided_conditions=[{"statement": "在只读账号下同样成立"}],
        )
        assert appended.condition_keys
        row = await service.get(created.weakness_id)
        assert row is not None and row.status == "confirmed"


async def test_relations_are_scoped_idempotent_and_version_checked(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        findings = FindingService(db)
        basis = await findings.upsert_weakness(
            session_id=session_id,
            title="提供低权会话",
            weakness_kind="enabling_condition",
            provided_conditions=[{"statement": "存在只读账号"}],
        )
        lead = await LeadService(db).upsert(
            session_id=session_id, hypothesis="只读账号可能能写", next_action="用只读账号 POST"
        )
        result = await upsert_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(basis.weakness_id),
                    role="basis",
                    condition_key=basis.condition_keys[0],
                    source_weakness_revision=basis.revision,
                )
            ],
        )
        assert len(result.links) == 1
        assert result.links[0].relation_state == "proposed"
        # 重复写入同一关系：不产生第二行，也不重复推进版本。
        again = await upsert_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(basis.weakness_id),
                    role="basis",
                    condition_key=basis.condition_keys[0],
                    source_weakness_revision=basis.revision,
                )
            ],
        )
        assert again.links[0].created is False
        rows = list(
            (
                await db.execute(
                    select(LeadWeaknessLinkRow).where(
                        LeadWeaknessLinkRow.session_id == session_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        # 过期来源版本必须明确冲突，而不是静默升级为新版本判断。
        await findings.upsert_weakness(
            session_id=session_id,
            weakness_id=basis.weakness_id,
            expected_revision=basis.revision,
            provided_conditions=[{"statement": "存在只读账号且可访问内部接口"}],
        )
        with pytest.raises(ApiError) as stale:
            await upsert_links(
                db,
                session_id=session_id,
                lead_id=lead.id,
                refs=[
                    WeaknessRelationRef(
                        weakness_id=str(basis.weakness_id),
                        role="basis",
                        source_weakness_revision=basis.revision,
                    )
                ],
            )
        assert stale.value.code is ErrorCode.CONFLICT
        affected = await leads_affected_by_weakness(db, session_id, basis.weakness_id)
        assert [row.id for row in affected] == [lead.id]


async def test_refute_marks_relation_without_cascading(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        findings = FindingService(db)
        weakness = await findings.upsert_weakness(session_id=session_id, title="可疑条件")
        lead = await LeadService(db).upsert(
            session_id=session_id, hypothesis="依赖该条件的路线", next_action="验证条件"
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
        refuted = await refute_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            weakness_ids=[str(weakness.weakness_id)],
        )
        assert [item.relation_state for item in refuted] == ["refuted"]
        links = await links_for_lead(db, session_id, lead.id)
        assert all(link.relation_state == str(LeadWeaknessRelationState.REFUTED) for link in links)
        # 下游线索**不**被自动判为 disproved：只标记待复核。
        refreshed = await LeadService(db).get(lead.id)
        assert refreshed is not None and refreshed.status == "new"


async def test_merge_repoints_relations_and_expires_judgements(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        findings = FindingService(db)
        weakness = await findings.upsert_weakness(session_id=session_id, title="条件弱点")
        leads = LeadService(db)
        source = await leads.upsert(
            session_id=session_id, hypothesis="重复假设（来源）", next_action="验证 X"
        )
        target = await leads.upsert(
            session_id=session_id, hypothesis="重复假设（主）", next_action="验证 X"
        )
        await leads.assess_actionability(
            target.id, state=str(Actionability.READY), note="已有明确下一步"
        )
        await upsert_links(
            db,
            session_id=session_id,
            lead_id=source.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id),
                    role="basis",
                    source_weakness_revision=weakness.revision,
                )
            ],
        )
        await leads.merge(session_id, source.id, target.id)
        moved = await links_for_lead(db, session_id, target.id)
        assert [link.lead_id for link in moved] == [target.id]
        assert await links_for_lead(db, session_id, source.id) == []
        # 合并改变了前置条件与关系：旧判断降级为 needs_review，而不是继续声称可推进。
        refreshed = await leads.get(target.id)
        assert refreshed is not None
        assert (refreshed.actionability_assessment or {}).get("state") == str(
            Actionability.NEEDS_REVIEW
        )


async def test_combining_two_weaknesses_stays_a_candidate(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    from agent_workbench.contracts.enums import FindingStatus
    from agent_workbench.domain.findings import VerificationService

    async with database.transaction() as db:
        findings = FindingService(db)
        verifier = VerificationService(db)
        first = await findings.upsert_weakness(session_id=session_id, title="缺陷 A")
        second = await findings.upsert_weakness(session_id=session_id, title="缺陷 B")
        for item in (first, second):
            await verifier.record(
                finding_id=item.weakness_id,
                method_version="fixture-v1",
                conclusion=FindingStatus.CONFIRMED,
                input_evidence_refs=[],
                performed_by="code",
            )
        combined = await findings.upsert_weakness(
            session_id=session_id,
            title="A 与 B 组合后可越权",
            description="组合假设",
        )
        lead = await LeadService(db).upsert(
            session_id=session_id, hypothesis="A+B 组合可越权", next_action="组合重放"
        )
        # 确认会推进版本；关系必须引用**当前**版本，不能沿用创建时的版本。
        first_row = await findings.get(first.weakness_id)
        second_row = await findings.get(second.weakness_id)
        assert first_row is not None and second_row is not None
        await upsert_links(
            db,
            session_id=session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(first.weakness_id),
                    role="basis",
                    source_weakness_revision=first_row.revision,
                ),
                WeaknessRelationRef(
                    weakness_id=str(second.weakness_id),
                    role="basis",
                    source_weakness_revision=second_row.revision,
                ),
            ],
        )
        row = await findings.get(combined.weakness_id)
        assert row is not None
        assert row.status == "candidate", "两侧分别 confirmed 不能让组合自动 confirmed"
        links = await links_for_lead(db, session_id, lead.id)
        assert len(links) == 2
        assert all(link.relation_state == "proposed" for link in links)
