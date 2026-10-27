"""最小循环的可选工作记录与候选发现工具。

计划只写 checkpoint；假设、观察和候选写业务表。复杂关系入口仅保留历史
兼容，未注册给生产模型。模型不能直接写 verified/confirmed；旧操作身份
和请求摘要保持不变，防止恢复时产生新的逻辑副作用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import (
    FindingCategory,
    LeadStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.plan import PlanStepPatch
from agent_workbench.contracts.weaknesses import (
    ActionabilityAssessment,
    WeaknessRelationRef,
)
from agent_workbench.domain.assets import record_asset
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.leads import LeadService, lead_summary
from agent_workbench.domain.observations import record_observation_ref
from agent_workbench.domain.weakness_relations import refute_links, upsert_links
from agent_workbench.observability.events import append_event
from agent_workbench.tools.operation import OperationRegistry, PreparedOperation
from agent_workbench.tools.params import coerce_str_list
from agent_workbench.tools.runtime import Toolbox


def _parse_json(raw: Any, field: str) -> Any:
    """解析 JSON 字符串；已经解析好的结构直接返回（模型偶尔直接给对象）。"""
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} 不是合法 JSON：{exc}") from exc


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #


@tool("update_plan")
async def update_plan(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    expected_plan_revision: int,
    steps: list[PlanStepPatch] | None = None,
    focus: str | None = None,
    rationale: str | None = None,
) -> Command:
    """可选地局部更新工作计划或当前调查方向，不是工具执行的前提。

    Args:
        expected_plan_revision: 上下文中的当前版本；过期写入被拒绝。
        steps: 只提交要新增或修改的步骤。每项含 step_id，可选 description/status/rationale/lead_id。
            新步骤必须有 description。status 为 pending/in_progress/done/dropped/blocked。
            省略的步骤和字段保持原样；完成只表示工作记录，不代表漏洞验证。
        focus: 可选的简短调查方向，不依赖步骤或线索；空字符串清除焦点。
        rationale: 可选简短修订说明。
        tool_call_id: 框架注入的稳定调用 ID。

    无需为普通反思、切换或完成步骤提交证明。同批只能一次计划更新。
    """
    from agent_workbench.tools.planning import apply_plan_update

    return await apply_plan_update(
        runtime,
        tool_call_id,
        steps=steps,
        focus=focus,
        rationale=rationale,
        expected_plan_revision=expected_plan_revision,
    )


# --------------------------------------------------------------------------- #
# 线索
# --------------------------------------------------------------------------- #


@tool("record_lead")
async def record_lead(
    runtime: ToolRuntime[RunContext],
    hypothesis: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    lead_id: str | None = None,
    next_action: str | None = None,
    status: str | None = None,
    note: str | None = None,
    evidence_refs: list[str] | None = None,
    expected_revision: int | None = None,
    url: str | None = None,
    method: str | None = None,
) -> str:
    """可选地保存或更新调查假设，无需为每次工具调用登记线索。

    Args:
        hypothesis: 简短调查假设，不是已验证漏洞。
        lead_id: 更新已有记录时填写。
        next_action: 可选下一动作。
        status: 可选 new/investigating/needs_verification/disproved/blocked/deferred，不接受 confirmed。
        note: 可选受阻、放弃或反证说明。
        evidence_refs: 可选真实工件 ID。
        expected_revision: 更新已有记录的可选乐观锁。
        url: 可选关联入口。
        method: 无法从来源推导时的采集方法。
        tool_call_id: 框架注入。
    """
    return await cast(Any, legacy_record_lead).coroutine(
        runtime,
        hypothesis=hypothesis,
        tool_call_id=tool_call_id,
        lead_id=lead_id,
        next_action=next_action,
        status=status,
        blocked_reason=note,
        evidence_refs_json=evidence_refs,
        expected_revision=expected_revision,
        url=url,
        method=method,
    )


@tool("record_lead")
async def legacy_record_lead(
    runtime: ToolRuntime[RunContext],
    hypothesis: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    impact: int | None = None,
    evidence_strength: int | None = None,
    feasibility: int | None = None,
    information_gain: int | None = None,
    url: str | None = None,
    identity_refs_json: list[str] | str | None = None,
    next_action: str | None = None,
    evidence_refs_json: list[str] | str | None = None,
    lead_id: str | None = None,
    status: str | None = None,
    blocked_reason: str | None = None,
    resume_condition: str | None = None,
    merge_into_id: str | None = None,
    observation_summary: str | None = None,
    source_operation_id: str | None = None,
    method: str | None = None,
    source_weaknesses_json: list[Any] | str | None = None,
    result_weaknesses_json: list[Any] | str | None = None,
    refuted_weaknesses_json: list[str] | str | None = None,
    required_conditions_json: list[Any] | str | None = None,
    actionability_json: dict[str, Any] | str | None = None,
    expected_revision: int | None = None,
) -> str:
    """登记或更新一条**假设（线索）**，并写清它的弱点依据与前置条件。

    Args:
        hypothesis: 假设内容，例如"该接口可能缺少服务端权限校验"。
        tool_call_id: 框架注入的重放身份，不要自行填写。
        impact: 影响程度 0-5。
        evidence_strength: 已有证据强度 0-5（仅凭猜测请填 0-1）。
        feasibility: 可执行性 0-5（当前条件下能否验证）。
        information_gain: 预期信息增益 0-5（验证后能排除多少不确定性）。
        url: 关联的入口 URL（可选，会按真实采集方法登记资产）。
        identity_refs_json: 关联身份标识的 JSON 数组字符串。
        next_action: 下一步具体动作。
        evidence_refs_json: 支持该线索的工件 ID 的 JSON 数组字符串。
        lead_id: 更新已有线索时填写。
        status: 可选，设置为 new/investigating/needs_verification/confirmed/disproved/
            blocked/deferred。
        blocked_reason: blocked/deferred 时**必填**。
        resume_condition: 恢复条件（什么条件下可以继续推进）。
        merge_into_id: 真正重复线索的目标 ID，同会话且禁止循环；原来源保留。
        observation_summary: 可选关键观察，与线索同一事务联记，必须引用真实工件。
        source_operation_id: 可选；产生这些证据的真实操作 ID。系统据此读取真实
            方法/入口/身份，而不是默认成 GET。
        method: 可选；当无法从证据推导时显式声明采集方法。
        source_weaknesses_json: `basis` 关系：这条线索从哪些弱点/条件获得依据。
            每项 `{"weakness_id": "...", "condition_key": "可选", "source_weakness_revision": 3,
              "requirement_key": "可选", "evidence_refs": ["工件 ID"]}`。
        result_weaknesses_json: `result` 关系：这条线索的验证实际产生/更新了哪些弱点（同格式）。
        refuted_weaknesses_json: 把与该线索的指定弱点关系标记为 `refuted`
            （弱点被反证）。**不**级联判定下游线索失败。
        required_conditions_json: 前置条件。每项
            `{"condition_key": "可选", "description": "...", "weakness_id": "可选",
              "producer_lead_id": "可选（另一条线索预期产出该条件）", "satisfied": false}`。
            引用另一条线索只表示**依赖意图**，不伪造一个已经存在的弱点。
            只有确实需要用户或环境提供时才设置 external=true；可由探索获取的条件保持 false。
        actionability_json: 当前能否推进的判断：
            `{"state": "ready|needs_condition|no_new_action|needs_review", "note": "短说明",
              "evidence_refs": ["工件 ID"], "operation_refs": ["操作 ID"],
              "unknowns": ["尚有哪些未知"]}`。`no_new_action` 必须写 `unknowns`；
            `ready` 必须已有 `next_action`。
        expected_revision: 更新已有线索时的乐观锁；过期写入返回明确冲突。

    Returns:
        线索记录摘要（JSON），包含关系写入结果与可推进性判断。

    Notes:
        - 评分仅供旧接口辅助展示，不作为必填或硬调度依据；
        - `confirmed` 只能由确定性核验器写入，这里**不接受**把线索直接标为 confirmed；
        - 关系写入与线索变更在同一事务；失败整体撤销，不留下半条关系。
    """
    toolbox: Toolbox = runtime.context.toolbox
    identity_refs = coerce_str_list(identity_refs_json, "identity_refs_json")
    evidence_refs = coerce_str_list(evidence_refs_json, "evidence_refs_json")
    basis_refs = _parse_relation_refs(
        source_weaknesses_json, role="basis", field="source_weaknesses_json"
    )
    result_refs = _parse_relation_refs(
        result_weaknesses_json, role="result", field="result_weaknesses_json"
    )
    refuted = coerce_str_list(refuted_weaknesses_json, "refuted_weaknesses_json")
    conditions = _parse_object_list(required_conditions_json, "required_conditions_json")
    actionability = _parse_actionability(actionability_json)

    target_status: LeadStatus | None = None
    if status:
        try:
            target_status = LeadStatus(status)
        except ValueError as exc:
            raise ValueError(f"status 非法：{status}") from exc
        if target_status is LeadStatus.CONFIRMED:
            raise ValueError(
                "confirmed 必须由确定性核验器写入；请先 record_observation + 运行核验，"
                "再把线索标为 needs_verification 并提交证据。"
            )

    async with toolbox.db.transaction() as db:
        from sqlalchemy import text as sql_text

        await db.execute(
            sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"record-lead:{toolbox.run_id}:{tool_call_id}"},
        )
        operation = await OperationRegistry(db).prepare(
            run_id=toolbox.run_id,
            session_id=toolbox.session_id,
            tool_call_id=tool_call_id,
            tool_name="record_lead",
            effect_class=ToolEffectClass.IDEMPOTENT_WRITE,
            policy_revision=getattr(toolbox, "policy_revision", 1),
            scope_revision=getattr(toolbox, "scope_revision", 1),
            dispatch_control_version=runtime.state.get("dispatch_control_version"),
            arguments={
                "hypothesis": hypothesis,
                "lead_id": lead_id,
                "url": url,
                "identity_refs": identity_refs,
                "evidence_refs": evidence_refs,
                "next_action": next_action,
                "status": status,
                "blocked_reason": blocked_reason,
                "resume_condition": resume_condition,
                "merge_into_id": merge_into_id,
                "observation_summary": observation_summary,
                "source_operation_id": source_operation_id,
                "method": method,
                "impact": impact,
                "evidence_strength": evidence_strength,
                "feasibility": feasibility,
                "information_gain": information_gain,
                **(
                    {
                        "expected_revision": expected_revision,
                        "source_weaknesses": [item.model_dump() for item in basis_refs],
                        "result_weaknesses": [item.model_dump() for item in result_refs],
                        "refuted_weaknesses": refuted,
                        "required_conditions": conditions,
                        "actionability": actionability.model_dump()
                        if actionability is not None
                        else None,
                    }
                    if any(
                        value is not None
                        for value in (
                            expected_revision,
                            source_weaknesses_json,
                            result_weaknesses_json,
                            refuted_weaknesses_json,
                            required_conditions_json,
                            actionability_json,
                        )
                    )
                    else {}
                ),
            },
        )
        if operation.row.result_snapshot is not None:
            return json.dumps(operation.row.result_snapshot, ensure_ascii=False, sort_keys=True)
        if observation_summary and not evidence_refs:
            raise ValueError("关键观察必须引用真实可用工件")
        if evidence_refs:
            from agent_workbench.persistence.tables import ArtifactRow

            for ref in evidence_refs:
                artifact = await db.get(ArtifactRow, UUID(ref))
                if (
                    artifact is None
                    or artifact.session_id != toolbox.session_id
                    or artifact.source_operation_id is None
                    or artifact.availability != "available"
                    or not toolbox.artifacts.verify(artifact.relative_key, artifact.sha256)
                ):
                    raise ValueError("线索证据必须为当前会话真实工具工件")
        asset_id: UUID | None = None
        asset_note: str | None = None
        evidence_context = await _evidence_operation_context(
            db,
            toolbox,
            [str(item) for item in evidence_refs],
            source_operation_id=source_operation_id,
        )
        if url:
            asset_id, asset_note = await _link_asset(
                db,
                toolbox,
                url=url,
                explicit_method=method,
                identity_refs=[str(item) for item in identity_refs],
                evidence_context=evidence_context,
                notes="由 record_lead 关联",
            )

        service = LeadService(db)
        row = await service.upsert(
            session_id=toolbox.session_id,
            hypothesis=hypothesis,
            asset_id=asset_id,
            identity_refs=[str(item) for item in identity_refs],
            impact=impact,
            evidence_strength=evidence_strength,
            feasibility=feasibility,
            information_gain=information_gain,
            next_action=next_action,
            evidence_refs=[str(item) for item in evidence_refs],
            lead_id=UUID(lead_id) if lead_id else None,
            required_conditions=conditions if required_conditions_json is not None else None,
            expected_revision=expected_revision,
        )
        if target_status is not None:
            row = await service.set_status(
                row.id,
                target_status,
                reason=blocked_reason,
                resume_condition=resume_condition,
            )
        relation_payload: dict[str, Any] | None = None
        if basis_refs or result_refs or refuted:
            for ref in [*basis_refs, *result_refs]:
                if ref.evidence_refs:
                    await _verify_artifact_refs(db, toolbox, ref.evidence_refs)
            batch = await upsert_links(
                db,
                session_id=toolbox.session_id,
                lead_id=row.id,
                refs=[*basis_refs, *result_refs],
                source_operation_id=operation.operation_id,
            )
            refuted_results = await refute_links(
                db,
                session_id=toolbox.session_id,
                lead_id=row.id,
                weakness_ids=refuted,
            )
            relation_payload = batch.to_dict()
            relation_payload["refuted"] = [
                *relation_payload["refuted"],
                *(item.to_dict() for item in refuted_results),
            ]
        if actionability is not None:
            if actionability.evidence_refs:
                await _verify_artifact_refs(db, toolbox, actionability.evidence_refs)
            row = await service.assess_actionability(
                row.id,
                state=actionability.state,
                note=actionability.note,
                evidence_refs=actionability.evidence_refs,
                operation_refs=actionability.operation_refs,
                unknowns=actionability.unknowns,
                source_tool_call_id=tool_call_id,
            )
        payload = lead_summary(row)
        observation_id = None
        if observation_summary:
            observation_id = await record_observation_ref(
                db,
                session_id=toolbox.session_id,
                source_run_id=toolbox.run_id,
                summary=observation_summary,
                artifact_ids=evidence_refs,
                source_operation_id=operation.operation_id,
                url=url,
            )
        if merge_into_id:
            row = await service.merge(toolbox.session_id, row.id, UUID(merge_into_id))
            payload = lead_summary(row)
        result = {
            "ok": True,
            "lead": payload,
            "relations": relation_payload,
            "operation_id": str(operation.operation_id),
            "observation_id": str(observation_id) if observation_id else None,
            "evidence_source_operation_ids": evidence_context["source_operation_ids"],
            "evidence_source_ambiguous": evidence_context["ambiguous"],
            "asset_note": asset_note,
        }
        operation.row.result_snapshot = result
        operation.row.status = "succeeded"

        if relation_payload:
            for link in [*relation_payload["links"], *relation_payload["refuted"]]:
                if link.get("changed"):
                    await append_event(
                        db,
                        session_id=toolbox.session_id,
                        run_id=toolbox.run_id,
                        event_type=EventType.RELATION_CHANGED,
                        payload={
                            key: link.get(key)
                            for key in (
                                "link_id",
                                "revision",
                                "lead_id",
                                "finding_id",
                                "role",
                                "relation_state",
                            )
                        },
                    )

    runtime.context.note_progress(f"lead:{payload['lead_id']}:{payload['status']}")
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# 观察
# --------------------------------------------------------------------------- #


@tool("record_observation")
async def record_observation(
    runtime: ToolRuntime[RunContext],
    summary: str,
    artifact_ids_json: list[str] | str | None = None,
    identity: str = "anonymous",
    url: str | None = None,
    category: str = "other",
    source_operation_id: str | None = None,
    method: str | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> str:
    """可选记录关键观察和资产来源；无需重复登记每次工具结果。

    Args:
        summary: 观察到的现象（客观描述，不写结论）。
        artifact_ids_json: 支撑该观察的工件 ID 的 JSON 数组字符串。**必须**来自真实工具结果。
        identity: 采集身份。
        url: 关联入口。
        category: 检查类别，例如 info_disclosure / auth_boundary / access_control /
            input_validation / configuration / transport / session_management / other。
        source_operation_id: 可选；产生这些证据的真实操作 ID。系统据此读取真实
            方法/入口/身份，而不是默认成 GET。
        method: 可选；当无法从证据推导时显式声明采集方法。
        tool_call_id: 框架注入，用于返回原重放回执。

    Returns:
        写入结果摘要（JSON）。

    Notes:
        - 观察 != 发现。观察是"我看到了什么"，发现是"这意味着什么"；
        - 引用不存在的工件会被拒绝——证据必须真实可读。
    """
    toolbox: Toolbox = runtime.context.toolbox
    artifact_ids = coerce_str_list(artifact_ids_json, "artifact_ids_json")
    resolved = await _verify_artifacts_exist(toolbox, artifact_ids)

    async with toolbox.db.transaction() as db:
        receipt = await _prepare_domain_receipt(
            db,
            toolbox,
            runtime,
            tool_call_id,
            "record_observation",
            {
                "summary": summary,
                "artifact_ids": resolved,
                "identity": identity,
                "url": url,
                "category": category,
                "source_operation_id": source_operation_id,
                "method": method,
            },
        )
        if receipt.row.result_snapshot is not None:
            return json.dumps(receipt.row.result_snapshot, ensure_ascii=False, sort_keys=True)
        asset_note: str | None = None
        evidence_context = await _evidence_operation_context(
            db, toolbox, resolved, source_operation_id=source_operation_id
        )
        if url:
            _, asset_note = await _link_asset(
                db,
                toolbox,
                url=url,
                explicit_method=method,
                identity_refs=[identity] if identity else [],
                evidence_context=evidence_context,
                notes="由 record_observation 关联",
            )

        ref = await record_observation_ref(
            db,
            session_id=toolbox.session_id,
            source_run_id=toolbox.run_id,
            summary=summary,
            artifact_ids=resolved,
            identity=identity,
            url=url,
            source_operation_id=receipt.operation_id,
        )
        payload = {
            "observation_id": str(ref),
            "artifacts": resolved,
            "category": category,
            "evidence_source_operation_ids": evidence_context["source_operation_ids"],
            "evidence_source_ambiguous": evidence_context["ambiguous"],
            "asset_note": asset_note,
        }
        result = {"ok": True, "observation": payload}
        receipt.row.result_snapshot = result
        receipt.row.status = "succeeded"

    runtime.context.note_progress(f"obs:{ref}")
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# 弱点（Finding 的升级形态）
# --------------------------------------------------------------------------- #


@tool("record_weakness")
async def record_weakness(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    weakness_id: str | None = None,
    expected_revision: int | None = None,
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    url: str | None = None,
    evidence_refs_json: list[str] | str | None = None,
    impact: str | None = None,
    weakness_kind: str | None = None,
    applicability_json: dict[str, Any] | str | None = None,
    provided_conditions_json: list[Any] | str | None = None,
    remove_condition_keys_json: list[str] | str | None = None,
    connection_review_json: dict[str, Any] | str | None = None,
    source_operation_id: str | None = None,
    method: str | None = None,
) -> str:
    """登记一个**弱点**（候选）：可独立成立的缺陷，或可供后续利用的条件。

    Args:
        tool_call_id: 框架注入，用于返回原重放回执。
        weakness_id: 补充已有弱点时填写；留空表示新建。
        expected_revision: 补充已有弱点时的乐观锁；与当前版本不一致会返回冲突，
            不覆盖新的证据和条件。
        title: 一句话标题（新建必填）。
        description: 是什么、在哪里、怎么观察到的。
        category: info_disclosure / auth_boundary / access_control / input_validation /
            configuration / transport / session_management / other。
        url: 关联入口（会按真实采集方法登记资产）。
        evidence_refs_json: 支持该弱点的工件 ID 的 JSON 数组字符串。**必须**引用真实工件。
        impact: 影响说明。
        weakness_kind: `vulnerability`（独立缺陷）/ `enabling_condition`（为后续利用提供
            条件）/ `unclassified`（尚未判定）。
        applicability_json: 适用范围：`{"asset_ids": [], "identity_ids": [],
            "prerequisites": [], "limitations": []}`。复用已有资产/身份，不新建身份库。
        provided_conditions_json: 有证据引用的条件陈述。每项
            `{"key": "可选（沿用已有条件时必须填）", "statement": "简短陈述",
              "evidence_refs": ["工件 ID"], "limitations": "限制"}`。
            证据引用说明**判断依据**，不自动证明陈述成立。省略 key 时由陈述派生稳定 key；
            修订已有条件请带上原 key。
        remove_condition_keys_json: 要删除的条件 key；删除被引用的条件前必须先处理引用。
        connection_review_json: 你对**本次写入后版本**后续用途的短判断：
            `{"linked_lead_ids": ["已产生的后续线索 ID"],
              "no_connection_note": "暂未找到后续用途时的短说明",
              "evidence_refs": ["工件 ID"]}`。
            `linked_lead_ids` 必须引用真实存在的关联；允许有依据地写"暂未发现用途"，
            但不要虚构连接。
        source_operation_id: 可选；产生这些证据的真实操作 ID。
        method: 可选；当无法从证据推导时显式声明采集方法。

    Returns:
        弱点写入摘要（JSON）：弱点 ID、版本、条件 key、实际新增/变化项、
        用途判断是否仍需重评。**不**返回长原始正文。

    Notes:
        - 你**不能**把它标记为 confirmed；确认必须由确定性核验记录支撑；
        - 请求成功、退出码 0、扫描器告警都**不是**确认依据；
        - 已确认记录的核心事实不可原地改写：发现新事实请新建候选并建立关系；
        - 同内容重复更新不会推进知识版本；只写用途判断也不会使该判断立刻过期。
    """
    return await _write_weakness(
        runtime,
        tool_call_id,
        tool_name="record_weakness",
        result_key="weakness",
        weakness_id=weakness_id,
        expected_revision=expected_revision,
        title=title,
        description=description,
        category=category,
        url=url,
        evidence_refs_json=evidence_refs_json,
        impact=impact,
        weakness_kind=weakness_kind,
        applicability_json=applicability_json,
        provided_conditions_json=provided_conditions_json,
        remove_condition_keys_json=remove_condition_keys_json,
        connection_review_json=connection_review_json,
        source_operation_id=source_operation_id,
        method=method,
    )


@tool("propose_finding")
async def propose_finding(
    runtime: ToolRuntime[RunContext],
    title: str,
    description: str,
    category: str = "other",
    url: str | None = None,
    evidence_refs_json: list[str] | str | None = None,
    impact: str | None = None,
    source_operation_id: str | None = None,
    method: str | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> str:
    """提出一条**候选发现**（candidate）。

    保存可选候选记录并保留实际来源；无需维护条件关系或后续用途判断。
    操作身份与旧请求摘要保持不变，重放不被当成新操作。

    Args:
        title: 一句话标题。
        description: 是什么、在哪里、怎么观察到的。
        category: info_disclosure / auth_boundary / access_control / input_validation /
            configuration / transport / session_management / other。
        url: 关联入口。
        evidence_refs_json: 支持该发现的工件 ID 的 JSON 数组字符串。**必须**引用真实工件。
        impact: 影响说明。
        source_operation_id: 可选；产生这些证据的真实操作 ID。系统据此读取真实
            方法/入口/身份，而不是默认成 GET。
        method: 可选；当无法从证据推导时显式声明采集方法。
        tool_call_id: 框架注入，用于返回原重放回执。

    Returns:
        候选发现摘要（JSON）。

    Notes:
        - 你**不能**把它标记为 confirmed。系统会检查是否有可用的验证记录；
        - 请求成功、退出码 0、扫描器告警都**不是**确认依据；
        - 没有可用证据时保留 candidate/inconclusive，并在说明里写清限制。
    """
    return await _write_weakness(
        runtime,
        tool_call_id,
        tool_name="propose_finding",
        result_key="finding",
        weakness_id=None,
        expected_revision=None,
        title=title,
        description=description,
        category=category,
        url=url,
        evidence_refs_json=evidence_refs_json,
        impact=impact,
        weakness_kind=None,
        applicability_json=None,
        provided_conditions_json=None,
        remove_condition_keys_json=None,
        connection_review_json=None,
        source_operation_id=source_operation_id,
        method=method,
        # 沿用旧实现的参数集合，保证历史调用重放时摘要不变。
        legacy_digest=True,
    )


async def _write_weakness(
    runtime: ToolRuntime[RunContext],
    tool_call_id: str,
    *,
    tool_name: str,
    result_key: str,
    weakness_id: str | None,
    expected_revision: int | None,
    title: str | None,
    description: str | None,
    category: str | None,
    url: str | None,
    evidence_refs_json: list[str] | str | None,
    impact: str | None,
    weakness_kind: str | None,
    applicability_json: dict[str, Any] | str | None,
    provided_conditions_json: list[Any] | str | None,
    remove_condition_keys_json: list[str] | str | None,
    connection_review_json: dict[str, Any] | str | None,
    source_operation_id: str | None,
    method: str | None,
    legacy_digest: bool = False,
) -> str:
    """弱点写入的**唯一**内部实现（新建与补充走同一条路径）。

    `legacy_digest` 仅供旧入口传入：它让操作**请求摘要**沿用旧实现的参数集合，
    否则历史调用重放会被判成"新操作"而冲突。
    """
    toolbox: Toolbox = runtime.context.toolbox
    refs = coerce_str_list(evidence_refs_json, "evidence_refs_json")
    resolved = await _verify_artifacts_exist(toolbox, refs)
    conditions = _parse_object_list(provided_conditions_json, "provided_conditions_json")
    review = _parse_optional_object(connection_review_json, "connection_review_json")
    applicability = _parse_optional_object(applicability_json, "applicability_json")
    remove_keys = coerce_str_list(remove_condition_keys_json, "remove_condition_keys_json")
    if weakness_kind is not None and str(weakness_kind) not in {
        "vulnerability",
        "enabling_condition",
        "unclassified",
    }:
        raise ValueError(f"weakness_kind 非法：{weakness_kind}")
    raw_category = category
    if category is not None:
        try:
            category = str(FindingCategory(category))
        except ValueError:
            category = str(FindingCategory.OTHER)
    parsed_weakness_id: UUID | None = None
    if weakness_id:
        try:
            parsed_weakness_id = UUID(str(weakness_id))
        except ValueError as exc:
            raise ValueError(f"weakness_id 不是合法 UUID：{weakness_id}") from exc

    # 摘要口径：旧入口必须沿用**旧实现的参数集合**，否则历史重放会被判成"新操作"；
    # 新入口才使用完整参数集合。
    receipt_arguments: dict[str, Any] = (
        {
            "title": title,
            "description": description,
            "category": raw_category,
            "url": url,
            "evidence_refs": resolved,
            "impact": impact,
            "source_operation_id": source_operation_id,
            "method": method,
        }
        if legacy_digest
        else {
            "weakness_id": weakness_id,
            "expected_revision": expected_revision,
            "title": title,
            "description": description,
            "category": category,
            "url": url,
            "evidence_refs": resolved,
            "impact": impact,
            "weakness_kind": weakness_kind,
            "applicability": applicability,
            "provided_conditions": conditions,
            "remove_condition_keys": remove_keys,
            "connection_review": review,
            "source_operation_id": source_operation_id,
            "method": method,
        }
    )
    async with toolbox.db.transaction() as db:
        receipt = await _prepare_domain_receipt(
            db,
            toolbox,
            runtime,
            tool_call_id,
            tool_name,
            receipt_arguments,
        )
        if receipt.row.result_snapshot is not None:
            return json.dumps(receipt.row.result_snapshot, ensure_ascii=False, sort_keys=True)
        asset_id: UUID | None = None
        asset_note: str | None = None
        evidence_context = await _evidence_operation_context(
            db, toolbox, resolved, source_operation_id=source_operation_id
        )
        if url:
            asset_id, asset_note = await _link_asset(
                db,
                toolbox,
                url=url,
                explicit_method=method,
                identity_refs=[],
                evidence_context=evidence_context,
                notes=f"由 {tool_name} 关联",
            )
        for item in conditions:
            item_refs = coerce_str_list(item.get("evidence_refs"), "evidence_refs")
            if item_refs:
                await _verify_artifact_refs(db, toolbox, item_refs)
        if review and review.get("evidence_refs"):
            await _verify_artifact_refs(
                db, toolbox, coerce_str_list(review.get("evidence_refs"), "evidence_refs")
            )

        write = await FindingService(db).upsert_weakness(
            session_id=toolbox.session_id,
            weakness_id=parsed_weakness_id,
            expected_revision=expected_revision,
            title=title,
            description=description,
            category=category,
            asset_id=asset_id,
            impact=impact,
            weakness_kind=weakness_kind,
            applicability=applicability,
            provided_conditions=conditions if provided_conditions_json is not None else None,
            remove_condition_keys=remove_keys,
            evidence_refs=resolved,
            connection_review=review,
        )
        row = await FindingService(db).get(write.weakness_id)
        if row is None:  # pragma: no cover - 刚写入的记录必然存在
            raise RuntimeError("弱点写入后无法读取，拒绝返回不一致回执")
        payload = {
            "weakness_id": str(row.id),
            "status": row.status,
            "weakness_kind": row.weakness_kind,
            "revision": write.revision,
            "content_changed": write.content_changed,
            "condition_keys": write.condition_keys,
            "added_condition_keys": write.added_condition_keys,
            "revised_condition_keys": write.revised_condition_keys,
            "removed_condition_keys": write.removed_condition_keys,
            "connection_review_required": write.review_required,
            "connection_review_stale": write.review_stale,
            "linked_lead_ids": write.linked_lead_ids,
            "evidence_refs": resolved,
            "evidence_source_operation_ids": evidence_context["source_operation_ids"],
            "evidence_source_ambiguous": evidence_context["ambiguous"],
            "asset_note": asset_note,
            "next_step": (
                "该记录只是候选。需要确定性核验后由系统写入验证记录才能成为 confirmed。"
                "请接着判断它的后续用途：如果它有条件可供后续利用，用 record_lead 建立"
                "有来源的线索；如果暂时没有可推进方向，也要写一句说明（不要虚构连接）。"
            ),
        }
        if legacy_digest:
            # 兼容入口保持旧字段名：历史对话里的模型只认 finding_id。
            payload["finding_id"] = str(row.id)
            payload["next_step"] = (
                "该记录只是候选。需要确定性核验后由系统写入验证记录，才能成为 confirmed。"
                "请在说明中写清验证方法与限制。"
            )
        result = {"ok": True, result_key: payload}
        receipt.row.result_snapshot = result
        receipt.row.status = "succeeded"
        if write.content_changed:
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.WEAKNESS_CHANGED,
                payload={
                    "weakness_id": str(row.id),
                    "revision": write.revision,
                    "changed_condition_keys": [
                        *write.added_condition_keys,
                        *write.revised_condition_keys,
                        *write.removed_condition_keys,
                    ],
                },
            )
        if review is not None:
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.WEAKNESS_REVIEWED,
                payload={
                    "weakness_id": str(row.id),
                    "basis_fingerprint": (row.connection_review or {}).get("basis_fingerprint"),
                    "linked_lead_ids": write.linked_lead_ids,
                    "disposition": "linked" if write.linked_lead_ids else "no_supported_connection",
                },
            )

    runtime.context.note_progress(f"{result_key}:{payload['weakness_id']}:{payload['revision']}")
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _parse_object_list(raw: list[Any] | str | None, field: str) -> list[dict[str, Any]]:
    """接受 JSON 字符串或列表，统一成对象列表。`None` 表示"未提供"。"""
    if raw is None:
        return []
    value: Any = _parse_json(raw, field) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是 JSON 数组")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{field} 第 {index} 项不是对象")
        out.append(dict(item))
    return out


def _parse_optional_object(raw: dict[str, Any] | str | None, field: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    value: Any = _parse_json(raw, field) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须为对象")
    return dict(value)


def _parse_relation_refs(
    raw: list[Any] | str | None, *, role: str, field: str
) -> list[WeaknessRelationRef]:
    refs: list[WeaknessRelationRef] = []
    for item in _parse_object_list(raw, field):
        payload = {**item, "role": role}
        payload.setdefault("evidence_refs", [])
        try:
            refs.append(WeaknessRelationRef.model_validate(payload))
        except Exception as exc:  # pydantic ValidationError
            raise ValueError(f"{field} 中的关系项不合法：{exc}") from exc
    return refs


def _parse_actionability(raw: dict[str, Any] | str | None) -> ActionabilityAssessment | None:
    payload = _parse_optional_object(raw, "actionability_json")
    if payload is None:
        return None
    try:
        return ActionabilityAssessment.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ValueError(f"actionability_json 不合法：{exc}") from exc


async def _verify_artifact_refs(db: Any, toolbox: Toolbox, raw_ids: list[str]) -> list[str]:
    """在**当前事务**内校验证据引用（关系/条件的依据也必须是真实工件）。"""
    from agent_workbench.persistence.tables import ArtifactRow

    resolved: list[str] = []
    for item in raw_ids:
        candidate = str(item).strip()
        if not candidate:
            continue
        try:
            artifact_id = UUID(candidate)
        except ValueError as exc:
            raise ValueError(f"证据引用不是工件 ID：{candidate}") from exc
        artifact = await db.get(ArtifactRow, artifact_id)
        if (
            artifact is None
            or artifact.session_id != toolbox.session_id
            or artifact.source_operation_id is None
            or artifact.availability != "available"
            or not toolbox.artifacts.verify(artifact.relative_key, artifact.sha256)
        ):
            raise ValueError("证据必须为当前会话真实工具工件")
        resolved.append(str(artifact.id))
    return resolved


async def _prepare_domain_receipt(
    db: Any,
    toolbox: Toolbox,
    runtime: ToolRuntime[RunContext],
    call_id: str,
    name: str,
    arguments: dict[str, Any],
) -> PreparedOperation:
    from sqlalchemy import text as sql_text

    call_id = call_id or str(runtime.tool_call_id or "")
    if not call_id:
        raise ValueError("领域写工具缺少稳定 tool_call_id，拒绝产生不可去重记录")
    await db.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"domain-receipt:{toolbox.run_id}:{call_id}"},
    )
    return await OperationRegistry(db).prepare(
        run_id=toolbox.run_id,
        session_id=toolbox.session_id,
        tool_call_id=call_id,
        tool_name=name,
        arguments=arguments,
        effect_class=ToolEffectClass.IDEMPOTENT_WRITE,
        policy_revision=getattr(toolbox, "policy_revision", 1),
        scope_revision=getattr(toolbox, "scope_revision", 1),
        dispatch_control_version=runtime.state.get("dispatch_control_version"),
    )


async def _verify_artifacts_exist(toolbox: Toolbox, raw_ids: list[str]) -> list[str]:
    """引用检查：不存在的工件会被拒绝，不允许"引用一个不存在的证据"。"""
    from sqlalchemy import select

    from agent_workbench.persistence.tables import ArtifactRow

    resolved: list[str] = []
    missing: list[str] = []
    async with toolbox.db.session() as db:
        for item in raw_ids:
            candidate = item.strip()
            if not candidate:
                continue
            try:
                artifact_id = UUID(candidate)
            except ValueError:
                missing.append(candidate)
                continue
            row = (
                await db.execute(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
            ).scalar_one_or_none()
            if (
                row is None
                or row.session_id != toolbox.session_id
                or row.availability != "available"
            ):
                missing.append(candidate)
                continue
            if not toolbox.artifacts.exists(row.relative_key):
                missing.append(candidate)
                continue
            resolved.append(str(row.id))
    if missing:
        raise ValueError(
            f"以下工件引用不可用（不存在、未同步或文件缺失）：{missing}。"
            "证据必须真实可读，请改用工具结果里返回的 artifact_id。"
        )
    return resolved


def _looks_like_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# 证据采集上下文（§4.2）
# --------------------------------------------------------------------------- #


async def _evidence_operation_context(
    db: Any,
    toolbox: Toolbox,
    evidence_refs: list[str],
    *,
    source_operation_id: str | None,
) -> dict[str, Any]:
    """从**真实采集操作**推导方法、入口与身份。

    规则：
    - 显式提供 `source_operation_id` 时优先读取该操作的真实上下文；
    - 否则从证据工件的 `source_operation_id` 推导，且**只在唯一时**采用；
    - 存在多个来源/多个方法时保留证据引用集合，**不任意选最后一次操作**作为全部来源。
    """
    from agent_workbench.persistence.tables import ArtifactRow, ToolOperationRow

    operation_ids: list[UUID] = []
    for ref in evidence_refs:
        artifact = await db.get(ArtifactRow, UUID(ref))
        if (
            artifact is not None
            and artifact.source_operation_id is not None
            and artifact.source_operation_id not in operation_ids
        ):
            operation_ids.append(artifact.source_operation_id)
    selected_id: UUID | None = None
    if source_operation_id:
        try:
            selected_id = UUID(str(source_operation_id))
        except ValueError as exc:
            raise ValueError(f"source_operation_id 不是合法 UUID：{source_operation_id}") from exc
        if operation_ids and selected_id not in operation_ids:
            raise ValueError("source_operation_id 不是所引用证据的采集来源")
        if selected_id not in operation_ids:
            operation_ids.append(selected_id)

    ambiguous = len(operation_ids) > 1
    if selected_id is None and len(operation_ids) != 1:
        return {
            "operation_id": None,
            "method": None,
            "entry_url": None,
            "identity_refs": [],
            "source_operation_ids": [str(item) for item in operation_ids],
            "ambiguous": ambiguous,
        }

    row = await db.get(ToolOperationRow, selected_id or operation_ids[0])
    if row is None or row.session_id != toolbox.session_id:
        raise ValueError("source_operation_id 不属于当前会话")
    context = row.execution_context or {}
    raw_method = context.get("method")
    return {
        "operation_id": row.operation_id,
        "method": str(raw_method).upper() if raw_method else None,
        "entry_url": context.get("url"),
        "identity_refs": [str(row.identity_id)]
        if row.identity_id is not None
        else [str(context["identity"])]
        if context.get("identity")
        else [],
        "source_operation_ids": [str(item) for item in operation_ids],
        "ambiguous": ambiguous,
    }


async def _link_asset(
    db: Any,
    toolbox: Toolbox,
    *,
    url: str | None,
    explicit_method: str | None,
    identity_refs: list[str],
    evidence_context: dict[str, Any],
    notes: str,
) -> tuple[UUID | None, str | None]:
    """按**真实采集上下文**关联端点资产；返回 `(asset_id, note)`。

    只提供 URL 而缺少采集方法时**不伪装成已验证 GET 证据**：
    不建立精确端点资产，改为让调用方只关联观察/线索，并给出明确说明。
    """
    if not url:
        return None, None
    provided_method = (explicit_method or "").strip().upper()
    captured_method = evidence_context.get("method")
    if captured_method and provided_method and captured_method != provided_method:
        raise ValueError("method 与真实采集操作不一致，请按来源操作关联")
    if evidence_context.get("entry_url") and evidence_context["entry_url"] != url:
        return None, "URL 与采集操作入口不同；保留证据引用，不把该操作的方法套用于另一个入口。"
    asset_method = captured_method or provided_method
    if not asset_method:
        return None, (
            "仅提供 URL 而缺少采集方法，未建立精确端点资产。"
            "请提供 method，或提供 source_operation_id / 引用工具返回的工件，"
            "让系统读取真实采集操作；否则请只关联观察而不建立端点。"
        )
    identity_ids = [item for item in identity_refs if _looks_like_uuid(item)]
    if not identity_ids:
        identity_ids = list(evidence_context.get("identity_refs") or [])
    asset, _created = await record_asset(
        db,
        session_id=toolbox.session_id,
        url=url,
        method=asset_method,
        identity_ids=identity_ids,
        notes=notes,
    )
    return asset.id, None


TASK_TOOLS = [
    update_plan,
    record_lead,
    record_observation,
    propose_finding,
]
