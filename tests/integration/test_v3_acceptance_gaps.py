"""验收发现的行为缺口：完整库存、真实关系、条件依据、发送游标与并发。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.agent.working_context import assemble_working_context
from agent_workbench.contracts.enums import LeadStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.weaknesses import WeaknessRelationRef
from agent_workbench.domain.exploration_review import (
    build_review_inventory,
    recall_by_condition_change,
)
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.weakness_relations import upsert_links
from agent_workbench.persistence.tables import (
    FindingRow,
    IdentityRow,
    LeadRow,
    SessionRow,
    ToolOperationRow,
)
from agent_workbench.runtime.stopping import FinishGate
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration
PROPOSAL = {"reason": "goal_answered", "goal_answer": "阶段结果", "stop_rationale": "无进一步动作"}


async def gate(database, session_id, **kwargs):
    async with database.session() as db:
        return await FinishGate(db).evaluate(session_id=session_id, proposal=PROPOSAL, **kwargs)


async def test_inventory_visits_each_uuid_once_beyond_first_batch(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        for index in range(405):
            db.add(
                LeadRow(
                    id=UUID(int=406 - index),
                    session_id=context.toolbox.session_id,
                    hypothesis=f"线索 {index}",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                )
            )
    async with database.session() as db:
        inventory = await build_review_inventory(db, context.toolbox.session_id)
        rows = await LeadService(db).iter_for_session(context.toolbox.session_id)
    assert inventory.total_objects == inventory.unreviewed_count == 405
    assert len(rows) == len({row.id for row in rows}) == 405


async def test_closed_leads_do_not_become_open_work_again(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        row = await leads.upsert(
            session_id=context.toolbox.session_id, hypothesis="已排除假设", next_action="旧的动作"
        )
        await leads.set_status(row.id, LeadStatus.DISPROVED, reason="已排除本假设的适用条件")
    assert (await gate(database, context.toolbox.session_id)).allowed


async def test_database_rejects_cross_session_relationship_even_without_service(
    database, unique_name
):
    from sqlalchemy.exc import IntegrityError

    from agent_workbench.persistence.tables import LeadWeaknessLinkRow

    context = await context_for(database, unique_name)
    other = await context_for(database, unique_name + "-other")
    async with database.transaction() as db:
        lead = await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="本会话线索"
        )
        weakness = await FindingService(db).upsert_weakness(
            session_id=other.toolbox.session_id, title="另一会话弱点"
        )
    with pytest.raises(IntegrityError):
        async with database.transaction() as db:
            db.add(
                LeadWeaknessLinkRow(
                    id=uuid4(),
                    session_id=context.toolbox.session_id,
                    lead_id=lead.id,
                    finding_id=weakness.weakness_id,
                    role="basis",
                )
            )
            await db.flush()


async def test_old_record_lead_receipt_replays_without_new_digest_or_write(database, unique_name):
    import json

    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.persistence.repositories import compute_request_digest
    from agent_workbench.tools.task import record_lead
    from tests.integration.test_chain_planning import _call

    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    arguments = {
        "hypothesis": "旧调用",
        "lead_id": None,
        "url": None,
        "identity_refs": [],
        "evidence_refs": [],
        "next_action": None,
        "status": None,
        "blocked_reason": None,
        "resume_condition": None,
        "merge_into_id": None,
        "observation_summary": None,
        "source_operation_id": None,
        "method": None,
        "impact": None,
        "evidence_strength": None,
        "feasibility": None,
        "information_gain": None,
    }
    old_receipt = {"ok": True, "lead": {"lead_id": str(uuid4()), "status": "new"}}
    async with database.transaction() as db:
        db.add(
            ToolOperationRow(
                operation_id=uuid4(),
                run_id=context.toolbox.run_id,
                session_id=context.toolbox.session_id,
                tool_call_id="legacy-lead",
                tool_name="record_lead",
                request_digest=compute_request_digest(
                    {"tool": "record_lead", "arguments": arguments}
                ),
                effect_class="idempotent_write",
                status="succeeded",
                result_snapshot=old_receipt,
            )
        )
    assert (
        json.loads(await _call(record_lead, context, {"hypothesis": "旧调用"}, "legacy-lead"))
        == old_receipt
    )
    async with database.session() as db:
        assert (
            await db.execute(
                select(LeadRow).where(LeadRow.session_id == context.toolbox.session_id)
            )
        ).scalars().all() == []


@pytest.mark.parametrize(
    "state,conditions",
    [
        ("no_new_action", []),
        ("needs_condition", []),
        ("needs_condition", [{"description": "待探索条件", "satisfied": False}]),
        ("ready", [{"description": "尚未满足", "satisfied": False}]),
    ],
)
async def test_labels_cannot_override_current_conditions(database, unique_name, state, conditions):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        row = await leads.upsert(
            session_id=context.toolbox.session_id,
            hypothesis="仍待判断",
            next_action="明确可做的动作",
            required_conditions=conditions,
        )
        await leads.assess_actionability(
            row.id, state=state, note="尝试收尾", unknowns=["尚有未知"]
        )
    result = await gate(database, context.toolbox.session_id)
    assert result.decision == "continue" and result.unreviewed_count == 1


async def test_waiting_count_uses_entire_inventory_not_current_page(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        leads = LeadService(db)
        for index in range(7):
            row = await leads.upsert(
                session_id=context.toolbox.session_id,
                hypothesis=f"外部条件 {index}",
                required_conditions=[{"description": "需用户提供环境", "external": True}],
            )
            await leads.assess_actionability(
                row.id, state="needs_condition", note="外部环境不可由本 Agent 补齐"
            )
    result = await gate(database, context.toolbox.session_id)
    assert result.decision == "wait_user" and result.blocked_count == 7


@pytest.mark.parametrize(
    "status,resume_note", [("pending", None), ("in_progress", None), ("blocked", None)]
)
async def test_unfinished_plan_cannot_be_ignored(database, unique_name, status, resume_note):
    context = await context_for(database, unique_name)
    result = await gate(
        database,
        context.toolbox.session_id,
        plan=[
            {
                "step_id": "s1",
                "description": "仍待推进",
                "status": status,
                "resume_note": resume_note,
            }
        ],
    )
    assert result.decision == "continue"


async def test_existing_unrelated_lead_is_not_a_connection(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        lead = await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="无关线索"
        )
        weakness = await FindingService(db).upsert_weakness(
            session_id=context.toolbox.session_id, title="孤立弱点"
        )
    with pytest.raises(ApiError, match="basis"):
        async with database.transaction() as db:
            await FindingService(db).upsert_weakness(
                session_id=context.toolbox.session_id,
                weakness_id=weakness.weakness_id,
                connection_review={"linked_lead_ids": [str(lead.id)]},
            )


async def test_referenced_condition_cannot_disappear_and_evidence_changes_revision(
    database, unique_name
):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        findings = FindingService(db)
        weakness = await findings.upsert_weakness(
            session_id=context.toolbox.session_id,
            title="来源条件",
            provided_conditions=[{"statement": "观察到条件"}],
        )
        lead = await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="依赖条件"
        )
        await upsert_links(
            db,
            session_id=context.toolbox.session_id,
            lead_id=lead.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id),
                    role="basis",
                    condition_key=weakness.condition_keys[0],
                )
            ],
        )
    with pytest.raises(ApiError, match="不能删除"):
        async with database.transaction() as db:
            await FindingService(db).upsert_weakness(
                session_id=context.toolbox.session_id,
                weakness_id=weakness.weakness_id,
                provided_conditions=[],
            )
    async with database.transaction() as db:
        changed = await FindingService(db).upsert_weakness(
            session_id=context.toolbox.session_id,
            weakness_id=weakness.weakness_id,
            provided_conditions=[
                {
                    "key": weakness.condition_keys[0],
                    "statement": "观察到条件",
                    "evidence_refs": [str(uuid4())],
                }
            ],
        )
    assert changed.revision == weakness.revision + 1


async def test_same_expected_revision_cannot_overwrite_concurrent_update(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        weakness = await FindingService(db).upsert_weakness(
            session_id=context.toolbox.session_id, title="并发来源"
        )

    async def update(description):
        try:
            async with database.transaction() as db:
                await FindingService(db).upsert_weakness(
                    session_id=context.toolbox.session_id,
                    weakness_id=weakness.weakness_id,
                    expected_revision=weakness.revision,
                    description=description,
                )
            return "ok"
        except ApiError:
            return "conflict"

    assert sorted(await asyncio.gather(update("修订 A"), update("修订 B"))) == ["conflict", "ok"]


async def test_recall_finds_unlinked_asset_and_each_new_source_revision(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        weakness = await FindingService(db).upsert_weakness(
            session_id=context.toolbox.session_id,
            title="共同特征",
            provided_conditions=[{"statement": "共同特征"}],
        )
        lead = await LeadService(db).upsert(
            session_id=context.toolbox.session_id,
            hypothesis="共同特征尚未验证",
            next_action="最小验证",
        )
    async with database.session() as db:
        first = await recall_by_condition_change(
            db, context.toolbox.session_id, revised_weakness_ids=[weakness.weakness_id]
        )
    assert [item.lead_id for item in first] == [str(lead.id)]
    async with database.transaction() as db:
        await FindingService(db).upsert_weakness(
            session_id=context.toolbox.session_id,
            weakness_id=weakness.weakness_id,
            description="条件修订",
        )
    async with database.session() as db:
        second = await recall_by_condition_change(
            db,
            context.toolbox.session_id,
            revised_weakness_ids=[weakness.weakness_id],
            seen_keys={first[0].dedupe_key},
        )
    assert second and second[0].dedupe_key != first[0].dedupe_key


async def test_presentation_commits_only_actual_items_and_recall_keys(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.context_manifest = {
        "presented_weakness_ids": ["shown"],
        "working_context": {
            "weakness_consumption": {"related_leads": [{"dedupe_key": "real-key"}]},
            "next": {"consumption_commit": {"weakness_revisions": {"shown": 2, "omitted": 3}}},
        },
    }
    update = await WorkingContextMiddleware().aafter_model(
        cast(Any, {"last_weakness_revisions": {"older": 1}}),
        cast(Any, SimpleNamespace(context=context)),
    )
    assert update["last_weakness_revisions"] == {"older": 1, "shown": 2}
    assert update["last_recall_keys"] == ["real-key"]


async def test_late_weakness_is_not_hidden_behind_old_reviewed_prefix(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        findings = FindingService(db)
        for index in range(485):
            await findings.upsert_weakness(
                session_id=context.toolbox.session_id,
                title=f"旧记录 {index}",
                connection_review={"no_connection_note": "已评估，没有支持进一步连接的条件"},
            )
        late = await findings.upsert_weakness(
            session_id=context.toolbox.session_id, title="后来的有效弱点"
        )
    async with database.session() as db:
        view = await assemble_working_context(db, context.toolbox.session_id, {})
    assert str(late.weakness_id) in [
        item["weakness_id"] for item in view["weakness_consumption"]["changed_weaknesses"]
    ]


async def test_invalid_operation_cannot_support_closure(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        row = await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="待排除"
        )
    with pytest.raises(ApiError, match="真实尝试"):
        async with database.transaction() as db:
            await LeadService(db).assess_actionability(
                row.id,
                state="no_new_action",
                note="已排除",
                unknowns=["尚有未知"],
                operation_refs=[str(uuid4())],
            )
    async with database.session() as db:
        assert (
            await db.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.session_id == context.toolbox.session_id
                )
            )
        ).scalars().all() == []


async def test_metadata_only_review_does_not_advance_knowledge_revision(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        findings = FindingService(db)
        original = await findings.upsert_weakness(
            session_id=context.toolbox.session_id, title="已判断弱点"
        )
        reviewed = await findings.upsert_weakness(
            session_id=context.toolbox.session_id,
            weakness_id=original.weakness_id,
            connection_review={"no_connection_note": "当前无依据支持后续用途"},
        )
        row = await db.get(FindingRow, original.weakness_id)
        assert reviewed.revision == original.revision and findings.review_is_current(row)


async def test_same_identity_auth_revision_invalidates_prior_assessment(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        owner = await db.get(SessionRow, context.toolbox.session_id)
        identity = IdentityRow(
            id=uuid4(), project_id=owner.project_id, display_name="验收身份", auth_revision=1
        )
        db.add(identity)
        await db.flush()
        leads = LeadService(db)
        row = await leads.upsert(
            session_id=context.toolbox.session_id,
            hypothesis="身份依赖",
            identity_refs=[str(identity.id)],
            next_action="复核身份条件",
        )
        await leads.assess_actionability(row.id, state="ready", note="当前身份条件可用")
    async with database.transaction() as db:
        saved = await db.get(IdentityRow, identity.id)
        saved.auth_revision += 1
    result = await gate(database, context.toolbox.session_id)
    assert result.unreviewed_count == 1 and result.decision == "continue"


async def test_completed_background_operation_recalls_bound_lead(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        lead = await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="后台证据待核对"
        )
        operation = ToolOperationRow(
            operation_id=uuid4(),
            run_id=context.toolbox.run_id,
            session_id=context.toolbox.session_id,
            tool_call_id="background-fixture",
            tool_name="http_request",
            request_digest="0" * 64,
            effect_class="read_only",
            status="succeeded",
            job_id=uuid4(),
            execution_context={"lead_ids": [str(lead.id)]},
        )
        db.add(operation)
    async with database.session() as db:
        recalled = await recall_by_condition_change(
            db, context.toolbox.session_id, completed_operation_ids=[operation.operation_id]
        )
    assert len(recalled) == 1 and recalled[0].trigger == "job_completed"
    async with database.session() as db:
        again = await recall_by_condition_change(
            db,
            context.toolbox.session_id,
            completed_operation_ids=[operation.operation_id],
            seen_keys={recalled[0].dedupe_key},
        )
    assert again == []


async def test_multi_lead_execution_preserves_each_relation_source_revision(database, unique_name):
    from agent_workbench.tools.execution_context import (
        context_from_state,
        weakness_revisions_for_focus,
    )

    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        findings = FindingService(db)
        weakness = await findings.upsert_weakness(
            session_id=context.toolbox.session_id, title="共享来源"
        )
        leads = LeadService(db)
        first = await leads.upsert(session_id=context.toolbox.session_id, hypothesis="第一条路线")
        second = await leads.upsert(session_id=context.toolbox.session_id, hypothesis="第二条路线")
        await upsert_links(
            db,
            session_id=context.toolbox.session_id,
            lead_id=first.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id), role="basis", source_weakness_revision=1
                )
            ],
        )
        await findings.upsert_weakness(
            session_id=context.toolbox.session_id,
            weakness_id=weakness.weakness_id,
            description="新增来源信息",
        )
        await upsert_links(
            db,
            session_id=context.toolbox.session_id,
            lead_id=second.id,
            refs=[
                WeaknessRelationRef(
                    weakness_id=str(weakness.weakness_id), role="basis", source_weakness_revision=2
                )
            ],
        )
    state = {
        "current_focus": {"step_id": "s1", "lead_id": str(first.id)},
        "plan": [
            {"step_id": "s1", "status": "in_progress", "lead_ids": [str(first.id), str(second.id)]}
        ],
    }
    async with database.session() as db:
        view = await assemble_working_context(db, context.toolbox.session_id, state)
    context.toolbox.working_context = view
    invocation = context_from_state(
        state,
        weakness_revisions=weakness_revisions_for_focus(context.toolbox),
        weakness_bindings=view["execution_source_bindings"],
    )
    assert invocation["lead_ids"] == [str(first.id), str(second.id)]
    sources = {item["lead_id"]: item for item in invocation["source_weakness_bindings"]}
    assert sources[str(first.id)]["source_revision"] == 1
    assert sources[str(second.id)]["source_revision"] == 2
    assert all(item["current_revision"] == 2 for item in sources.values())


async def test_no_new_action_with_real_attempt_and_cleared_action_can_finish(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        operation = ToolOperationRow(
            operation_id=uuid4(),
            run_id=context.toolbox.run_id,
            session_id=context.toolbox.session_id,
            tool_call_id="attempt-fixture",
            tool_name="http_request",
            request_digest="0" * 64,
            effect_class="read_only",
            status="failed",
        )
        db.add(operation)
        await db.flush()
        leads = LeadService(db)
        lead = await leads.upsert(
            session_id=context.toolbox.session_id,
            hypothesis="仍有未知但无有效动作",
            next_action="先前的尝试",
        )
        await leads.upsert(
            session_id=context.toolbox.session_id,
            lead_id=lead.id,
            hypothesis=lead.hypothesis,
            next_action="",
        )
        await leads.assess_actionability(
            lead.id,
            state="no_new_action",
            note="已有尝试已区分当前可测条件；其他未知需未来新证据",
            operation_refs=[str(operation.operation_id)],
            unknowns=["其他环境条件下是否成立尚未知"],
        )
    assert (await gate(database, context.toolbox.session_id)).allowed
