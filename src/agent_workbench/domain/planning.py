"""规划来源与完成检查。只读取领域权威记录，不选择行动或持有另一份计划。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.planning import (
    PlanningSource,
    PlanningValidationError,
    StepCompletionClaim,
    StepCompletionRecord,
    StepCompletionSpec,
    completion_review_reason,
    planning_digest,
)
from agent_workbench.domain.planning_applicability import check_applicability
from agent_workbench.domain.planning_http import check_http_work
from agent_workbench.persistence.tables import (
    ArtifactRow,
    FindingRow,
    IdentityRow,
    ObservationRow,
    RunRow,
    SessionRow,
    ToolOperationRow,
    VerificationRow,
)


async def context_signature(db: AsyncSession, session_id: UUID, run_id: UUID) -> str:
    task = await db.get(SessionRow, session_id, populate_existing=True)
    run = await db.get(RunRow, run_id, populate_existing=True)
    if task is None or run is None or run.session_id != session_id:
        raise ValueError("完成记录的 Run 不属于当前会话")
    identity = (
        await db.get(IdentityRow, task.active_identity_id) if task.active_identity_id else None
    )
    return planning_digest(
        {
            "run_id": str(run.id),
            "objective": run.objective,
            "control_version": run.control_version,
            "scope": task.scope_snapshot,
            "policy": task.policy_snapshot,
            "identity": str(task.active_identity_id),
            "auth_revision": identity.auth_revision if identity else None,
        }
    )


async def resolve_sources(
    db: AsyncSession,
    store: Any,
    session_id: UUID,
    refs: list[PlanningSource],
    *,
    step_ids: set[str] | None = None,
    terminal: bool = False,
) -> dict[str, Any]:
    """校验真实性与适用性，自动规范化跨步骤支持，不修改原始归属。

    无焦点的初始探索可以被显式引用；不会把它伪装成原来就绑定了该步骤。
    """
    snapshots: dict[str, Any] = {}
    operations: dict[str, ToolOperationRow] = {}
    artifacts: set[UUID] = set()
    task = await db.get(SessionRow, session_id, populate_existing=True)
    if task is None:
        raise ValueError("来源会话不存在")
    normalized: list[PlanningSource] = []
    source_links: list[dict[str, Any]] = []
    active_operations: set[str] = set()
    limitations: set[str] = set()

    async def operation(identifier: UUID) -> ToolOperationRow:
        key = str(identifier)
        row = operations.get(key) or await db.get(
            ToolOperationRow, identifier, populate_existing=True
        )
        if row is None or row.session_id != session_id:
            raise PlanningValidationError(
                "source_operation_unavailable",
                "来源操作不属于当前会话或不存在",
                recovery="使用已有回执中的 operation_id 或工件 ID；resource_id/job_id 不是 operation_id。不要换 UUID 或重放外部操作。",
                operation_id=key,
            )
        association = row.execution_context or {}
        if terminal and row.status != "succeeded":
            raise ValueError("来源操作未成功取得结果或状态未知，不能把执行失败当作调查阴性")
        notes = await check_applicability(db, task, row)
        limitations.update(notes)
        active_operations.add(key)
        operations[key] = row
        snapshots[f"operation:{key}"] = {
            "status": row.status,
            "request_digest": row.request_digest,
            "result_digest": planning_digest(row.result_snapshot),
            "identity_id": str(row.identity_id),
            "execution_context": association,
            "applicability_limits": notes,
        }
        return row

    async def artifact(identifier: UUID) -> None:
        row = await db.get(ArtifactRow, identifier, populate_existing=True)
        if (
            row is None
            or row.session_id != session_id
            or row.availability != "available"
            or row.source_operation_id is None
            or not store.verify(row.relative_key, row.sha256)
        ):
            raise ValueError("来源工件必须为当前会话真实、可读且哈希一致的执行工件")
        await operation(row.source_operation_id)
        artifacts.add(identifier)
        snapshots[f"artifact:{identifier}"] = {
            "sha256": row.sha256,
            "operation_id": str(row.source_operation_id),
            "truncated": row.truncated,
        }

    for ref in sorted(refs, key=lambda item: (item.kind, str(item.id))):
        active_operations.clear()
        if ref.kind == "operation":
            await operation(ref.id)
        elif ref.kind == "artifact":
            await artifact(ref.id)
        elif ref.kind == "observation":
            observation = await db.get(ObservationRow, ref.id, populate_existing=True)
            if (
                observation is None
                or observation.session_id != session_id
                or not observation.artifact_ids
            ):
                raise ValueError("观察缺少当前会话的真实工件来源")
            if observation.source_operation_id:
                await operation(observation.source_operation_id)
            for identifier in observation.artifact_ids:
                await artifact(UUID(identifier))
            snapshots[f"observation:{ref.id}"] = {
                "summary": observation.summary,
                "artifact_ids": observation.artifact_ids,
                "source_operation_id": str(observation.source_operation_id),
            }
        else:
            verification = await db.get(VerificationRow, ref.id, populate_existing=True)
            finding = (
                await db.get(FindingRow, verification.finding_id, populate_existing=True)
                if verification
                else None
            )
            if (
                verification is None
                or finding is None
                or finding.session_id != session_id
                or finding.verification_id != verification.id
                or finding.status != verification.conclusion
                or verification.performed_by != "code"
            ):
                raise ValueError("独立验证来源不可用、已被替代或不是程序验证")
            evidence = [*verification.input_evidence_refs, *verification.recheck_evidence_refs]
            if not evidence:
                raise ValueError("独立验证没有真实证据")
            for identifier in evidence:
                await artifact(UUID(identifier))
            snapshots[f"verification:{ref.id}"] = {
                "conclusion": verification.conclusion,
                "method_version": verification.method_version,
                "finding_revision": finding.revision,
                "finding_status": finding.status,
                "tested_conditions": verification.tested_conditions,
            }
        original_steps = sorted(
            {
                str((operations[key].execution_context or {}).get("step_id"))
                for key in active_operations
                if (operations[key].execution_context or {}).get("step_id")
            }
        )
        relation = (
            "supporting" if step_ids is not None and set(original_steps) - step_ids else "direct"
        )
        normalized.append(ref.model_copy(update={"relation": relation}))
        source_links.append(
            {
                "kind": ref.kind,
                "id": str(ref.id),
                "relation": relation,
                "operation_ids": sorted(active_operations),
                "source_step_ids": original_steps,
            }
        )
    return {
        "signature": planning_digest(
            {
                "snapshots": snapshots,
                "refs": sorted((ref.kind, str(ref.id), ref.relation) for ref in normalized),
            }
        ),
        "operations": operations,
        "snapshots": snapshots,
        "normalized_refs": normalized,
        "source_links": source_links,
        "limitations": sorted(limitations),
    }


async def check_step_completion(
    db: AsyncSession,
    store: Any,
    *,
    session_id: UUID,
    run_id: UUID,
    step: dict[str, Any],
    claim: StepCompletionClaim,
) -> dict[str, Any]:
    spec = StepCompletionSpec.model_validate(step.get("completion_spec"))
    refs = list(claim.source_refs)
    relation = "supporting" if any(ref.relation == "supporting" for ref in refs) else "direct"
    if spec.predicate is not None:
        refs.append(
            PlanningSource(kind="operation", id=spec.predicate.operation_id, relation=relation)
        )
    if spec.verification_id is not None:
        refs.append(PlanningSource(kind="verification", id=spec.verification_id, relation=relation))
    refs = list({(ref.kind, ref.id): ref for ref in refs}.values())
    if len(refs) > 8:
        raise ValueError("完成依据最多 8 个来源")
    resolved = await resolve_sources(
        db, store, session_id, refs, step_ids={step["step_id"]}, terminal=True
    )
    if spec.not_before is not None:
        older = [
            str(row.operation_id)
            for row in resolved["operations"].values()
            if row.created_at < spec.not_before
        ]
        if older:
            raise PlanningValidationError(
                "source_too_old",
                "引用结果早于任务声明的有效时间",
                recovery="选择符合 not_before 的已有来源；若确实没有，再决定是否补测。",
                target_step_id=step["step_id"],
                operation_ids=older[:8],
            )
    if spec.mode == "achieve" and any(
        "历史条件版本已变化" in note for note in resolved["limitations"]
    ):
        raise PlanningValidationError(
            "source_condition_review_required",
            "历史来源不能证明达成型任务的当前前置条件",
            recovery="先复核变化的条件；调查可以保留历史观察，不能把历史事实提升为当前目标已达成。",
            target_step_id=step["step_id"],
        )
    coverage = check_http_work(spec, resolved["operations"], step_id=step["step_id"])
    outcome = claim.outcome
    scope = "仅检查来源与状态一致性；完成语义由模型判断，不代表独立验证"
    if spec.predicate is not None:
        predicate = spec.predicate
        op = resolved["operations"][str(predicate.operation_id)]
        if op.status != "succeeded":
            raise ValueError("确定性字段检查需要成功且有结构化回执的操作")
        value: Any = op.result_snapshot
        for component in predicate.path:
            if not isinstance(value, dict) or component not in value:
                raise ValueError("完成谓词字段不存在，不能把未知当作阴性结果")
            value = value[component]
        if not isinstance(value, str | int | float | bool | type(None)):
            raise ValueError("完成谓词只支持结构化标量")
        expected = (
            "satisfied"
            if type(value) is type(predicate.equals) and value == predicate.equals
            else "not_satisfied"
        )
        if outcome != expected:
            raise ValueError("完成结果与持久化回执的字段检查不一致")
        scope = "仅证明指定操作回执的字段匹配，不扩张为漏洞确认或任意目标证明"
    elif spec.verification_id is not None:
        snapshot = resolved["snapshots"][f"verification:{spec.verification_id}"]
        expected = {"confirmed": "satisfied", "disproved": "not_satisfied"}.get(
            snapshot["conclusion"], "inconclusive"
        )
        if outcome != expected:
            raise ValueError("完成结果与独立验证结论不一致")
        scope = "只继承引用验证记录自身的测试范围与限制，不确认组合路径"
    if coverage:
        scope += f"；已匹配声明的 {len(coverage)} 项 HTTP 方法/端点回执，不核验自然语言范围、参数条件及正文含义"
    record = {
        **claim.model_dump(mode="json"),
        "source_refs": [ref.model_dump(mode="json") for ref in resolved["normalized_refs"]],
        "check": {
            "status": "accepted",
            "method": spec.check_type,
            "scope": scope,
            "spec_digest": planning_digest(spec.model_dump(mode="json")),
            "source_signature": resolved["signature"],
            "context_signature": await context_signature(db, session_id, run_id),
            "run_id": str(run_id),
            "version": 2,
            "source_authenticity": "checked",
            "source_applicability": "limited" if resolved["limitations"] else "checked",
            "sufficiency": {
                "model": "model_judgment",
                "predicate": "receipt_fields",
                "verification": "verification_record",
            }[spec.check_type],
            "http_coverage": coverage,
            "source_links": resolved["source_links"],
            "limitations": resolved["limitations"],
        },
    }
    record = StepCompletionRecord.model_validate(record).model_dump(mode="json")
    reason = completion_review_reason(
        {
            **step,
            "status": "done",
            "completion_protocol": 1,
            "legacy_completion": False,
            "completion": record,
        }
    )
    if reason:
        raise ValueError(reason)
    return record


async def completion_is_current(
    db: AsyncSession,
    store: Any,
    session_id: UUID,
    step: dict[str, Any],
    *,
    run_id: UUID | None = None,
) -> str | None:
    reason = completion_review_reason(step)
    if reason or step.get("status") != "done" or step.get("completion_protocol") != 1:
        return reason
    try:
        record = StepCompletionRecord.model_validate(step.get("completion"))
        if record.check.version == 1:
            return "旧完成记录需补充工作范围并复核；引用已有来源重新提交，不要自动重测"
        checked_run = UUID(record.check.run_id)
        if run_id is not None and checked_run != run_id:
            return "完成记录来自其他阶段，需要按当前目标复查"
        fresh = await check_step_completion(
            db,
            store,
            session_id=session_id,
            run_id=checked_run,
            step=step,
            claim=StepCompletionClaim.model_validate(record.model_dump(exclude={"check"})),
        )
        if fresh["check"] != record.check.model_dump(mode="json"):
            return "完成来源、身份、范围或指令已变化，需要复查"
    except ValueError as exc:
        return str(exc)
    return None
