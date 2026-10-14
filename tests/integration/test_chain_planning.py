"""弱点—关系—计划—执行在同一条链上的工具行为。"""

from __future__ import annotations

import inspect
import json
from typing import Any, cast
from uuid import UUID

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Command
from sqlalchemy import select

from agent_workbench.contracts.enums import Actionability
from agent_workbench.domain.exploration_review import derive_actionability
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.tables import FindingRow, LeadRow, LeadWeaknessLinkRow
from agent_workbench.tools.history import search_history
from agent_workbench.tools.task import record_lead, record_weakness, update_plan
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def _call(
    tool_obj: Any,
    context: Any,
    args: dict[str, Any],
    call_id: str,
    state: dict[str, Any] | None = None,
) -> Any:
    runtime = ToolRuntime(
        state=state or {},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=call_id,
        store=None,
    )
    function = cast(Any, tool_obj).coroutine
    parameters = inspect.signature(function).parameters
    if "tool_call_id" in parameters:
        args = {**args, "tool_call_id": call_id}
    return await function(runtime, **args)


async def test_weakness_relation_plan_and_history_stay_one_chain(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)

    created = json.loads(
        await _call(
            record_weakness,
            context,
            {
                "title": "错误页回显内部路径",
                "description": "不存在的路径返回带绝对路径的堆栈",
                "weakness_kind": "enabling_condition",
                "provided_conditions_json": [{"statement": "错误页包含绝对路径信息"}],
            },
            "weak-create",
        )
    )
    payload = created["weakness"]
    assert payload["status"] == "candidate"
    assert payload["connection_review_required"] is True
    assert payload["condition_keys"], "条件必须由领域层分配稳定 key"
    condition_key = payload["condition_keys"][0]

    # 重放同一调用：返回原回执，不重复创建。
    replay = json.loads(
        await _call(
            record_weakness,
            context,
            {
                "title": "错误页回显内部路径",
                "description": "不存在的路径返回带绝对路径的堆栈",
                "weakness_kind": "enabling_condition",
                "provided_conditions_json": [{"statement": "错误页包含绝对路径信息"}],
            },
            "weak-create",
        )
    )
    assert replay == created
    async with database.session() as db:
        findings = (
            (await db.execute(select(FindingRow).where(FindingRow.session_id == context.toolbox.session_id)))
            .scalars()
            .all()
        )
    assert len(findings) == 1

    lead_result = json.loads(
        await _call(
            record_lead,
            context,
            {
                "hypothesis": "可借错误页路径做目录穿越",
                "next_action": "用 ../../ 探测静态目录",
                "source_weaknesses_json": [
                    {
                        "weakness_id": payload["weakness_id"],
                        "condition_key": condition_key,
                        "source_weakness_revision": payload["revision"],
                    }
                ],
                "required_conditions_json": [
                    {
                        "description": "需要一个无需认证即可访问的静态目录",
                        "satisfied": False,
                    }
                ],
                "actionability_json": {
                    "state": "needs_condition",
                    "note": "缺未认证静态目录入口",
                },
            },
            "lead-create",
        )
    )
    lead_id = lead_result["lead"]["lead_id"]
    assert lead_result["relations"]["links"][0]["role"] == "basis"
    assert lead_result["lead"]["actionability"] == "needs_condition"
    assert lead_result["lead"]["required_conditions"][0]["condition_key"]
    async with database.session() as db:
        links = (
            (await db.execute(select(LeadWeaknessLinkRow).where(LeadWeaknessLinkRow.session_id == context.toolbox.session_id)))
            .scalars()
            .all()
        )
    assert [str(link.lead_id) for link in links] == [lead_id]

    # 计划：一个步骤服务多条线索，且类型区分覆盖与跟进。
    planned = await _call(
        update_plan,
        context,
        {
            "steps_json": [
                {
                    "step_id": "s1",
                    "description": "沿错误页路径推进",
                    "status": "in_progress",
                    "kind": "followup",
                    "lead_ids": [lead_id],
                },
                {
                    "step_id": "s2",
                    "description": "补检未覆盖入口",
                    "status": "pending",
                    "kind": "coverage",
                },
            ],
            "rationale": "建立链条",
        },
        "plan-update",
    )
    assert isinstance(planned, Command)
    assert planned.update is not None
    plan = planned.update["plan"]
    assert plan[0]["kind"] == "followup" and plan[0]["lead_ids"] == [lead_id]
    assert plan[0]["lead_id"] == lead_id, "旧 lead_id 兼容投影必须保留"
    assert plan[1]["lead_ids"] == []
    assert planned.update["current_focus"]["lead_id"] == lead_id

    # 用途判断：引用真实存在的关联才算完成。
    reviewed = json.loads(
        await _call(
            record_weakness,
            context,
            {
                "weakness_id": payload["weakness_id"],
                "expected_revision": payload["revision"],
                "connection_review_json": {"linked_lead_ids": [lead_id]},
            },
            "weak-review",
        )
    )["weakness"]
    assert reviewed["connection_review_required"] is False
    assert reviewed["linked_lead_ids"] == [lead_id]

    detail = json.loads(
        await _call(
            search_history,
            context,
            {"kind": "weakness", "record_id": payload["weakness_id"]},
            "history-detail",
        )
    )
    assert detail["weakness"]["id"] == payload["weakness_id"]
    assert detail["related_leads"][0]["lead_id"] == lead_id
    assert detail["relations"][0]["role"] == "basis"


async def test_connection_review_must_reference_real_links(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    created = json.loads(
        await _call(
            record_weakness,
            context,
            {"title": "候选弱点", "description": "说明"},
            "weak-1",
        )
    )["weakness"]
    with pytest.raises(Exception) as error:
        await _call(
            record_weakness,
            context,
            {
                "weakness_id": created["weakness_id"],
                "expected_revision": created["revision"],
                "connection_review_json": {
                    "linked_lead_ids": ["00000000-0000-0000-0000-000000000000"]
                },
            },
            "weak-bad-review",
        )
    assert "真实存在" in str(error.value)
    # 空判断不算判断。
    with pytest.raises(Exception) as empty:
        await _call(
            record_weakness,
            context,
            {
                "weakness_id": created["weakness_id"],
                "expected_revision": created["revision"],
                "connection_review_json": {},
            },
            "weak-empty-review",
        )
    assert "短说明" in str(empty.value)


async def test_record_lead_rejects_unsupported_actionability_and_plan_conflicts(
    database, unique_name
):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    with pytest.raises(Exception) as error:
        await _call(
            record_lead,
            context,
            {
                "hypothesis": "无新动作但没有依据",
                "actionability_json": {"state": "no_new_action", "note": "没有别的方法"},
            },
            "lead-no-unknowns",
        )
    assert "unknowns" in str(error.value)

    with pytest.raises(Exception) as ready_error:
        await _call(
            record_lead,
            context,
            {
                "hypothesis": "声称可以推进",
                "actionability_json": {"state": "ready", "note": "下一步明确"},
            },
            "lead-ready",
        )
    assert "next_action" in str(ready_error.value)

    lead = json.loads(
        await _call(
            record_lead,
            context,
            {"hypothesis": "准备工作", "next_action": "先探测入口"},
            "lead-prep",
        )
    )["lead"]
    with pytest.raises(Exception) as conflict:
        await _call(
            update_plan,
            context,
            {
                "steps_json": [
                    {
                        "step_id": "s1",
                        "description": "冲突步骤",
                        "status": "pending",
                        "lead_id": lead["lead_id"],
                        "lead_ids": ["11111111-1111-1111-1111-111111111111"],
                    }
                ]
            },
            "plan-conflict",
        )
    assert "冲突" in str(conflict.value)

    async with database.session() as db:
        stored = (
            (await db.execute(select(LeadRow).where(LeadRow.session_id == context.toolbox.session_id)))
            .scalars()
            .all()
        )
    assert [row.hypothesis for row in stored] == ["准备工作"]
    assert stored[0].actionability_assessment is None, "失败的判断不能留下半条记录"


async def test_legacy_propose_finding_keeps_operation_identity_and_fields(database, unique_name):
    """兼容入口必须沿用**旧实现的参数集合**：否则崩溃后重放会变成"新操作"。"""
    from agent_workbench.persistence.repositories import compute_request_digest
    from agent_workbench.persistence.tables import ToolOperationRow
    from agent_workbench.tools.task import propose_finding

    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    args = {"title": "旧格式", "description": "旧调用", "category": "other"}
    first = json.loads(await _call(propose_finding, context, args, "legacy-finding"))
    assert first["finding"]["finding_id"] == first["finding"]["weakness_id"], "旧字段名必须保留"
    assert await _call(propose_finding, context, args, "legacy-finding") == json.dumps(
        first, ensure_ascii=False, sort_keys=True
    )
    async with database.session() as db:
        row = (
            await db.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.run_id == context.toolbox.run_id,
                    ToolOperationRow.tool_call_id == "legacy-finding",
                )
            )
        ).scalar_one()
    legacy_arguments = {
        "title": "旧格式",
        "description": "旧调用",
        "category": "other",
        "url": None,
        "evidence_refs": [],
        "impact": None,
        "source_operation_id": None,
        "method": None,
    }
    assert row.tool_name == "propose_finding"
    assert row.request_digest == compute_request_digest(
        {"tool": "propose_finding", "arguments": legacy_arguments}
    ), "重放口径必须与旧实现逐字段一致"


async def test_weakness_scan_and_review_are_not_truncated_by_display_limits(
    database, unique_name
):
    """展示有上限，但**结束复查集合**必须完整（超过展示上限也要发现）。"""
    from agent_workbench.agent.working_context import assemble_working_context
    from agent_workbench.domain.findings import FindingService
    from agent_workbench.runtime.stopping import FinishGate

    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        service = FindingService(db)
        for index in range(210):
            await service.upsert_weakness(session_id=session_id, title=f"弱点 {index}")
    async with database.session() as db:
        view = await assemble_working_context(db, session_id, {"objective": "上限验收"})
        gate = await FinishGate(db).evaluate(
            session_id=session_id,
            proposal={
                "reason": "goal_answered",
                "goal_answer": "完成",
                "stop_rationale": "没有方向",
            },
        )
    assert view["weakness_consumption"]["pending_connection_review_count"] == 210
    assert gate.unreviewed_count == 210, "复查集合不能按展示上限截断"
    assert not gate.allowed


async def test_explicit_ready_condition_resolution_keeps_deferred_lead_actionable(
    database, unique_name
):
    """`deferred + ready` 表示"可以做但排在后面"：仍然阻止自主结束。"""
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    lead = json.loads(
        await _call(
            record_lead,
            context,
            {
                "hypothesis": "暂缓但可做",
                "next_action": "用已知凭据重放",
                "status": "deferred",
                "blocked_reason": "当前阶段优先级较低",
                "resume_condition": "当前主线完成后继续",
                "actionability_json": {"state": str(Actionability.READY), "note": "条件已具备"},
            },
            "lead-deferred-ready",
        )
    )["lead"]
    assert lead["status"] == "deferred"
    assert lead["actionability"] == "ready"

    async with database.session() as db:
        row = await db.get(LeadRow, UUID(lead["lead_id"]))
        assert row is not None
        resolution = derive_actionability(row, stale=False)
    assert resolution.actionable is True, "deferred 不是自动终态"
