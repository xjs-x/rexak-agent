"""证据复用的确定性边界；不改写执行归属，不判断漏洞是否成立。"""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.contracts.planning import PlanningValidationError, planning_digest
from agent_workbench.domain.scope import ScopePolicy, ScopeViolation
from agent_workbench.persistence.tables import FindingRow, IdentityRow, SessionRow, ToolOperationRow


def condition_snapshot(row: FindingRow) -> dict[str, Any]:
    return {
        "status": row.status,
        "applicability": row.applicability or {},
        "conditions": {str(item["key"]): item for item in row.provided_conditions or []},
    }


async def check_applicability(
    db: AsyncSession, task: SessionRow, row: ToolOperationRow
) -> list[str]:
    """当前授权仍须允许来源；新条件的增加不反向撤销旧条件。"""
    context = row.execution_context or {}
    notes: list[str] = []
    recovery = "保留已有操作和工件；按当前条件复查来源，不要换 UUID 或盲目重测。"

    def reject(code: str, message: str) -> NoReturn:
        raise PlanningValidationError(
            code,
            message,
            recovery=recovery,
            operation_id=str(row.operation_id),
            source_step_id=context.get("step_id"),
        )

    policy_revision = int((task.policy_snapshot or {}).get("revision", 1))
    if row.policy_revision != policy_revision:
        reject("source_policy_changed", "来源采集时的执行策略已变化，需要按当前策略复查")
    url = context.get("url")
    if url:
        try:
            snapshot = ScopeSnapshot.model_validate(task.scope_snapshot)
            ScopePolicy(snapshot).check_execution_url(str(url))
        except (ScopeViolation, ValueError):
            reject("source_outside_scope", "来源目标已不在当前授权范围内，不能用于本次完成")
        mapping = context.get("target_mapping_signature")
        if mapping is not None and mapping != planning_digest(snapshot.target_mappings):
            reject(
                "source_target_mapping_changed",
                "来源采集后的目标映射已变化，不能把旧目标结果作为新目标事实",
            )
        if mapping is None and row.scope_revision != snapshot.revision:
            reject("source_scope_history_unknown", "历史来源没有目标映射快照且授权版本已变化")
        if row.tool_name == "http_request" and not context.get("authentication"):
            notes.append("历史 HTTP 来源缺少认证方式快照；身份标签不能独立证明匿名访问")
    elif row.scope_revision != int((task.scope_snapshot or {}).get("revision", 1)):
        reject("source_scope_changed", "来源缺少可核对的目标，且授权版本已变化")
    elif row.tool_name not in {"record_observation", "record_lead", "record_weakness"}:
        notes.append("非 HTTP 来源的目标及结果语义仍由模型判断")

    if row.identity_id:
        identity = await db.get(IdentityRow, row.identity_id, populate_existing=True)
        if identity is None or (identity.session_id != task.id and identity.id != task.active_identity_id):
            reject("source_identity_unavailable", "来源身份不属于当前会话或已不可用")
        assert identity is not None
        captured = context.get("identity_auth_revision")
        if captured is not None and captured != identity.auth_revision:
            reject("source_identity_changed", "来源采集后的身份认证状态已变化")
        if captured is None:
            notes.append("历史来源没有认证版本快照；仅保留采集时身份，不证明当前登录能力")

    for identifier, expected in context.get("source_weakness_revisions", {}).items():
        finding = await db.get(FindingRow, UUID(identifier), populate_existing=True)
        previous = context.get("source_condition_snapshots", {}).get(identifier)
        if finding is None or finding.session_id != task.id:
            reject("source_condition_unavailable", "来源引用的条件已不可用")
        assert finding is not None
        if previous:
            current = condition_snapshot(finding)
            unchanged = (
                current["status"] not in {"disproved", "retracted"}
                and current["applicability"] == previous.get("applicability")
                and all(
                    current["conditions"].get(key) == value
                    for key, value in previous.get("conditions", {}).items()
                )
            )
            if not unchanged:
                reject("source_condition_changed", "来源所依赖的既有条件被否定、撤回或修改")
        elif finding.revision != expected:
            notes.append("历史条件版本已变化：结果只证明采集时的观察，不证明当前前置条件仍成立")
    return sorted(set(notes))
