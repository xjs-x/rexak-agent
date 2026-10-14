"""v1/v2 到当前版本的显式兼容适配，使用官方状态更新接口，保留节点边界。

迁移链：1 -> 2 -> 3，**不允许跳过**。每次迁移都保留消息 ID、操作 ID、计划与待执行
节点；不把所有旧步骤自动标完成，也不重写历史操作的焦点来美化关联率（§5.4）。
"""

from __future__ import annotations

from typing import Any

from agent_workbench.agent.state import (
    CURRENT_STATE_SCHEMA_VERSION,
    MIGRATABLE_STATE_SCHEMA_VERSIONS,
    StateSchemaIncompatibleError,
    assert_compatible_state,
)
from agent_workbench.agent.working_context import assemble_working_context


def needs_migration(values: dict[str, Any]) -> bool:
    version = values.get("state_schema_version")
    return type(version) is int and version in MIGRATABLE_STATE_SCHEMA_VERSIONS


def adapt_v1(
    values: dict[str, Any],
    projection: dict[str, Any],
    *,
    checkpoint_id: str,
    next_nodes: tuple[str, ...],
) -> dict[str, Any]:
    if type(values.get("state_schema_version")) is not int or values["state_schema_version"] != 1:
        raise StateSchemaIncompatibleError("只支持显式 v1 到 v2 适配，拒绝恢复未知状态")
    # 不重写 messages、原 ID、计划、摘要来源或完成计数。
    return {
        "state_schema_version": 2,
        "current_focus": values.get("current_focus"),
        "review_cursor": values.get("review_cursor"),
        "last_review_turn": values.get("last_review_turn", 0),
        "active_lead_ids": projection["next"]["selected_ids"],
        "pending_job_ids": projection["pending_job_ids"],
        "state_migration": {
            "adapter": "v1_to_v2_visible_state_v1",
            "from": 1,
            "to": 2,
            "source_checkpoint_id": checkpoint_id,
            "expected_next": list(next_nodes),
        },
    }


def adapt_v2(
    values: dict[str, Any],
    *,
    checkpoint_id: str,
    next_nodes: tuple[str, ...],
) -> dict[str, Any]:
    """v2 到 v3：只补充新语义字段，不重写历史焦点与计划。

    - `current_focus.source`：已有焦点标为 `explicit`（那是模型当轮明确写入的），
      没有焦点时保持 None；**不**为历史步骤自动造焦点；
    - `work_assessment` 置空：v2 没有这个对象，不从旧摘要倒推"最近判断"。
    """
    if type(values.get("state_schema_version")) is not int or values["state_schema_version"] != 2:
        raise StateSchemaIncompatibleError("只支持显式 v2 到 v3 适配，拒绝恢复未知状态")
    focus = values.get("current_focus")
    if focus is not None and not focus.get("source"):
        focus = {**focus, "source": "explicit"}
    return {
        "state_schema_version": 3,
        "current_focus": focus,
        "work_assessment": None,
        "similar_result_streak": 0,
        "last_similar_signature": "",
        "state_migration": {
            "adapter": "v2_to_v3_focus_and_assessment_v1",
            "from": 2,
            "to": 3,
            "source_checkpoint_id": checkpoint_id,
            "expected_next": list(next_nodes),
        },
    }


async def _apply_update(
    graph: Any, snapshot: Any, update: dict[str, Any], read_config: dict[str, Any]
) -> Any:
    # 使用有 checkpoint_id 的配置作原边界更新，不编辑框架序列化表。
    await graph.aupdate_state(snapshot.config, update)
    return await graph.aget_state(read_config)


def _assert_migration_preserved(before: dict[str, Any], migrated: Any) -> None:
    if tuple(migrated.next) != tuple(before.get("next", ())):
        raise StateSchemaIncompatibleError("迁移改变了待执行节点，拒绝恢复；需核对原 checkpoint")
    if [(m.id, m.type, m.content) for m in migrated.values.get("messages", [])] != [
        (m.id, m.type, m.content) for m in before.get("messages", [])
    ]:
        raise StateSchemaIncompatibleError("迁移消息完整性校验失败，拒绝恢复")


async def ensure_current_checkpoint(
    graph: Any, config: dict[str, Any], snapshot: Any, database: Any, session_id: Any
) -> Any:
    values = dict(snapshot.values or {})
    if not values:
        return snapshot
    if values.get("state_migration"):
        # 只有迁移刚产生的 checkpoint 需要比较 expected_next；后续正常图节点可以变化。
        marker = values["state_migration"]
        parent_id = ((snapshot.parent_config or {}).get("configurable") or {}).get("checkpoint_id")
        if parent_id == marker.get("source_checkpoint_id") and list(snapshot.next) != marker.get(
            "expected_next"
        ):
            raise StateSchemaIncompatibleError("迁移边界校验失败，拒绝恢复；请检查原 checkpoint")

    version = values.get("state_schema_version")
    if version == 1:
        # 部分并行工具已完成但尚未提交的 pending writes 不能由更新接口猜测重放顺序。
        if any(getattr(task, "result", None) is not None for task in (snapshot.tasks or ())):
            raise StateSchemaIncompatibleError(
                "v1 checkpoint 含未提交的并行结果，拒绝恢复；需先核对操作台账"
            )
        async with database.session() as db:
            projection = await assemble_working_context(db, session_id, values)
        source_id = str(snapshot.config["configurable"]["checkpoint_id"])
        migrated = await _apply_update(
            graph,
            snapshot,
            adapt_v1(values, projection, checkpoint_id=source_id, next_nodes=tuple(snapshot.next)),
            config,
        )
        _assert_migration_preserved(
            {"next": snapshot.next, "messages": values.get("messages", [])}, migrated
        )
        values = dict(migrated.values or {})
        snapshot = migrated
        version = values.get("state_schema_version")

    if version == 2:
        if any(getattr(task, "result", None) is not None for task in (snapshot.tasks or ())):
            raise StateSchemaIncompatibleError(
                "v2 checkpoint 含未提交的并行结果，拒绝迁移；需先核对操作台账"
            )
        source_id = str(snapshot.config["configurable"]["checkpoint_id"])
        migrated = await _apply_update(
            graph,
            snapshot,
            adapt_v2(values, checkpoint_id=source_id, next_nodes=tuple(snapshot.next)),
            config,
        )
        _assert_migration_preserved(
            {"next": snapshot.next, "messages": values.get("messages", [])}, migrated
        )
        snapshot = migrated
        values = dict(migrated.values or {})

    assert_compatible_state(values)
    if values.get("state_schema_version") != CURRENT_STATE_SCHEMA_VERSION:
        raise StateSchemaIncompatibleError("迁移后状态版本仍不是当前版本，拒绝恢复")
    return snapshot


__all__ = [
    "adapt_v1",
    "adapt_v2",
    "ensure_current_checkpoint",
    "needs_migration",
]
