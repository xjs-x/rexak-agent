"""任务资料工具：`update_plan`、`record_lead`、`record_observation`、`propose_finding`。

规则（§8.2）：
- 输入校验 + 来源检查；
- **模型不能直接标记 verified**；
- 计划写入图状态（LangGraph checkpoint 为权威）；线索/观察/发现写入业务表；
- 模型猜测只形成 `candidate`，`confirmed` 只能由验证服务写入。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.agent.state import PlanStep, WorkAssessment
from agent_workbench.contracts.enums import (
    CoverageStatus,
    FindingCategory,
    LeadStatus,
    ToolEffectClass,
)
from agent_workbench.domain.assets import record_asset
from agent_workbench.domain.findings import CoverageService, FindingService
from agent_workbench.domain.leads import LeadService, lead_summary
from agent_workbench.domain.observations import record_observation_ref
from agent_workbench.tools.operation import OperationRegistry, PreparedOperation
from agent_workbench.tools.params import coerce_str_list
from agent_workbench.tools.runtime import Toolbox

PLAN_STEP_STATUSES = {"pending", "in_progress", "done", "dropped", "blocked"}
MAX_PLAN_STEPS = 20
#: 轻量判断的负载初值：单字段最多 500 字、引用最多 8 个。
MAX_ASSESSMENT_CHARS = 500
MAX_ASSESSMENT_REFS = 8


def _parse_json(raw: str, field: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} 不是合法 JSON：{exc}") from exc


def _tool_message(tool_call_id: str, text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=tool_call_id, name="plan")


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #


@tool("update_plan")
async def update_plan(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    steps_json: str | None = None,
    rationale: str | None = None,
    focus_json: str | None = None,
    assessment_json: str | None = None,
) -> Command:
    """更新**当前计划**、焦点与最近判断（有界、可修订，不是预先锁死的 DAG）。

    Args:
        steps_json: 计划步骤的 JSON 数组字符串。每项：
            `{"step_id": "s1", "description": "...", "status": "pending|in_progress|done|dropped|blocked",
              "rationale": "为什么做这一步", "lead_id": "可选关联线索"}`。
            **不传表示保留现有计划**；显式 `[]` 才是清空。
        rationale: 本次计划变更的原因（会被记录，便于复盘方向变化）。
        focus_json: 可选焦点对象，含 step_id、lead_id、intent、reason、return_to_step_id。
            不传时系统按"唯一进行中步骤"自动承接；多主线时会给一次简短提示。
        assessment_json: 可选**轻量判断**：
            `{"changed": "这次结果改变了什么", "next_action": "下一步消除哪个不确定性",
              "uncertainty": "未测到/条件不成立/假设受反证", "evidence_refs": ["工件 ID"]}`。
            只在关键转折时补充，不必每轮都写；纯计划调整可以不写。
        tool_call_id: 由框架注入，不要自己填写。

    Returns:
        更新确认（同时把计划、焦点与最近判断写回图状态）。
    """
    runtime.context.note_progress(f"plan:{rationale or ''}")
    steps: list[PlanStep] | None = None
    seen: set[str] = set()
    if steps_json is not None:
        raw = _parse_json(steps_json, "steps_json")
        if not isinstance(raw, list):
            raise ValueError("steps_json 必须是 JSON 数组")
        if len(raw) > MAX_PLAN_STEPS:
            raise ValueError(f"计划最多 {MAX_PLAN_STEPS} 步；请合并或删除已完成步骤")
        built: list[PlanStep] = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 项不是对象")
            step_id = str(item.get("step_id") or f"s{index}")
            if step_id in seen:
                raise ValueError(f"step_id 重复：{step_id}")
            seen.add(step_id)
            status = str(item.get("status", "pending"))
            if status not in PLAN_STEP_STATUSES:
                raise ValueError(f"第 {index} 项 status 非法：{status}")
            description = str(item.get("description") or "").strip()
            if not description:
                raise ValueError(f"第 {index} 项缺少 description")
            if status == "blocked" and not item.get("rationale"):
                raise ValueError(f"第 {index} 项状态为 blocked，必须写 rationale（受阻原因）")
            built.append(
                PlanStep(
                    step_id=step_id,
                    description=description,
                    status=status,  # type: ignore[typeddict-item]
                    rationale=item.get("rationale"),
                    lead_id=item.get("lead_id"),
                    done_when=item.get("done_when"),
                    resume_note=item.get("resume_note"),
                    evidence_refs=coerce_str_list(item.get("evidence_refs"), "evidence_refs"),
                )
            )
        steps = built

    previous_plan = list(runtime.state.get("plan") or [])
    effective_plan = steps if steps is not None else previous_plan
    explicit_focus: dict[str, Any] | None = None
    if focus_json is not None:
        parsed_focus = _parse_json(focus_json, "focus_json")
        if not isinstance(parsed_focus, dict):
            raise ValueError("focus_json 必须为对象")
        if parsed_focus.get("lead_id"):
            async with runtime.context.toolbox.db.session() as db:
                lead = await LeadService(db).get(UUID(parsed_focus["lead_id"]))
                if lead is None or lead.session_id != runtime.context.toolbox.session_id:
                    raise ValueError("焦点线索不属于当前会话")
        explicit_focus = parsed_focus

    previous_focus = runtime.state.get("current_focus")
    focus, hint = resolve_focus(effective_plan, previous_focus, explicit_focus)
    focus_changed = focus_signature(focus) != focus_signature(previous_focus)

    update: dict[str, Any] = {"current_focus": focus}
    if steps is not None:
        update["plan"] = steps
        update["plan_revision"] = int(runtime.state.get("plan_revision") or 0) + 1
    assessment = None
    if assessment_json is not None:
        parsed_assessment = _parse_json(assessment_json, "assessment_json")
        if not isinstance(parsed_assessment, dict):
            raise ValueError("assessment_json 必须为对象")
        changed = str(parsed_assessment.get("changed") or "").strip()
        next_action = str(parsed_assessment.get("next_action") or "").strip()
        if not changed or not next_action:
            raise ValueError("判断必须同时说明 changed（改变了什么）与 next_action（下一步）")
        refs = coerce_str_list(parsed_assessment.get("evidence_refs"), "evidence_refs")
        if len(refs) > MAX_ASSESSMENT_REFS:
            raise ValueError(f"判断最多引用 {MAX_ASSESSMENT_REFS} 个工件")
        if refs:
            refs = await _verify_artifacts_exist(runtime.context.toolbox, refs)
        if len(changed) > MAX_ASSESSMENT_CHARS or len(next_action) > MAX_ASSESSMENT_CHARS:
            raise ValueError(f"判断字段最多 {MAX_ASSESSMENT_CHARS} 字")
        previous_assessment = runtime.state.get("work_assessment") or {}
        uncertainty = str(parsed_assessment.get("uncertainty") or "").strip() or None
        if uncertainty and len(uncertainty) > MAX_ASSESSMENT_CHARS:
            raise ValueError(f"判断字段最多 {MAX_ASSESSMENT_CHARS} 字")
        assessment = WorkAssessment(
            step_id=(focus or {}).get("step_id"),
            lead_id=(focus or {}).get("lead_id"),
            changed=changed,
            next_action=next_action,
            evidence_refs=refs,
            uncertainty=uncertainty,
            source_tool_call_id=tool_call_id,
            revision=int(previous_assessment.get("revision") or 0) + 1,
        )
        update["work_assessment"] = assessment

    text = json.dumps(
        {
            "ok": True,
            "plan_revision": update.get("plan_revision", runtime.state.get("plan_revision") or 0),
            "plan_changed": steps is not None,
            "current_focus": focus,
            "focus_changed": focus_changed,
            "focus_hint": hint,
            "assessment_revision": assessment["revision"] if assessment else None,
            "steps": len(effective_plan),
            "note": (
                "计划/焦点/最近判断已写入图状态（LangGraph checkpoint 为权威）；"
                "前端只做只读投影。自动承接的焦点只说明执行时的工作上下文，"
                "不证明证据支持某个假设。"
            ),
        },
        ensure_ascii=False,
    )
    update["messages"] = [_tool_message(tool_call_id, text)]
    return Command(update=update)


# --------------------------------------------------------------------------- #
# 线索
# --------------------------------------------------------------------------- #


@tool("record_lead")
async def record_lead(
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
) -> str:
    """登记或更新一条**假设（线索）**。

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

    Returns:
        线索记录摘要（JSON）。

    Notes:
        - 评分仅供旧接口辅助展示，不作为必填或硬调度依据；
        - `confirmed` 只能由确定性核验器写入，这里**不接受**把线索直接标为 confirmed。
    """
    toolbox: Toolbox = runtime.context.toolbox
    identity_refs = coerce_str_list(identity_refs_json, "identity_refs_json")
    evidence_refs = coerce_str_list(evidence_refs_json, "evidence_refs_json")

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
        )
        if target_status is not None:
            row = await service.set_status(
                row.id,
                target_status,
                reason=blocked_reason,
                resume_condition=resume_condition,
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
            "operation_id": str(operation.operation_id),
            "observation_id": str(observation_id) if observation_id else None,
            "evidence_source_operation_ids": evidence_context["source_operation_ids"],
            "evidence_source_ambiguous": evidence_context["ambiguous"],
            "asset_note": asset_note,
        }
        operation.row.result_snapshot = result
        operation.row.status = "succeeded"

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
    coverage_status: str = "checked_negative",
    source_operation_id: str | None = None,
    method: str | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> str:
    """记录一次**观察**：索引已有工件，并更新覆盖状态。

    Args:
        summary: 观察到的现象（客观描述，不写结论）。
        artifact_ids_json: 支撑该观察的工件 ID 的 JSON 数组字符串。**必须**来自真实工具结果。
        identity: 采集身份。
        url: 关联入口。
        category: 检查类别，例如 info_disclosure / auth_boundary / access_control /
            input_validation / configuration / transport / session_management / other。
        coverage_status: not_checked / checked_negative / checked_positive / blocked /
            not_applicable。
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

    try:
        coverage = CoverageStatus(coverage_status)
    except ValueError as exc:
        raise ValueError(f"coverage_status 非法：{coverage_status}") from exc

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
                "coverage_status": coverage_status,
                "source_operation_id": source_operation_id,
                "method": method,
            },
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
        await CoverageService(db).record(
            session_id=toolbox.session_id,
            check_category=category,
            status=coverage,
            asset_id=asset_id,
            entry_label=url or "",
            reason=summary[:500],
            evidence_refs=resolved,
        )
        payload = {
            "observation_id": str(ref),
            "artifacts": resolved,
            "coverage_status": coverage_status,
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
# 发现（候选）
# --------------------------------------------------------------------------- #


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
    toolbox: Toolbox = runtime.context.toolbox
    refs = coerce_str_list(evidence_refs_json, "evidence_refs_json")
    resolved = await _verify_artifacts_exist(toolbox, refs)
    try:
        parsed_category = FindingCategory(category)
    except ValueError:
        parsed_category = FindingCategory.OTHER

    async with toolbox.db.transaction() as db:
        receipt = await _prepare_domain_receipt(
            db,
            toolbox,
            runtime,
            tool_call_id,
            "propose_finding",
            {
                "title": title,
                "description": description,
                "category": category,
                "url": url,
                "evidence_refs": resolved,
                "impact": impact,
                "source_operation_id": source_operation_id,
                "method": method,
            },
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
                notes="由 propose_finding 关联",
            )

        row = await FindingService(db).propose(
            session_id=toolbox.session_id,
            title=title,
            description=description,
            category=parsed_category,
            asset_id=asset_id,
            evidence_refs=resolved,
            impact=impact,
        )
        payload = {
            "finding_id": str(row.id),
            "status": row.status,
            "evidence_refs": resolved,
            "evidence_source_operation_ids": evidence_context["source_operation_ids"],
            "evidence_source_ambiguous": evidence_context["ambiguous"],
            "asset_note": asset_note,
            "next_step": (
                "该记录只是候选。需要确定性核验后由系统写入验证记录，才能成为 confirmed。"
                "请在说明中写清验证方法与限制。"
            ),
        }
        result = {"ok": True, "finding": payload}
        receipt.row.result_snapshot = result
        receipt.row.status = "succeeded"

    runtime.context.note_progress(f"finding:{payload['finding_id']}")
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


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


TASK_TOOLS = [update_plan, record_lead, record_observation, propose_finding]
