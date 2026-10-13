"""v1 到 v2 的显式兼容适配，使用官方状态更新接口，保留节点边界。"""

from __future__ import annotations

from typing import Any

from agent_workbench.agent.state import StateSchemaIncompatibleError, assert_compatible_state
from agent_workbench.agent.working_context import assemble_working_context


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
    if type(values.get("state_schema_version")) is int and values["state_schema_version"] == 1:
        # 部分并行工具已完成但尚未提交的 pending writes 不能由更新接口猜测重放顺序。
        if any(getattr(task, "result", None) is not None for task in (snapshot.tasks or ())):
            raise StateSchemaIncompatibleError(
                "v1 checkpoint 含未提交的并行结果，拒绝恢复；需先核对操作台账"
            )
        async with database.session() as db:
            projection = await assemble_working_context(db, session_id, values)
        source_id = str(snapshot.config["configurable"]["checkpoint_id"])
        update = adapt_v1(
            values, projection, checkpoint_id=source_id, next_nodes=tuple(snapshot.next)
        )
        # 使用有 checkpoint_id 的配置作原边界更新，不编辑框架序列化表。
        await graph.aupdate_state(snapshot.config, update)
        migrated = await graph.aget_state(config)
        if tuple(migrated.next) != tuple(snapshot.next):
            raise StateSchemaIncompatibleError(
                "迁移改变了待执行节点，拒绝恢复；需核对原 checkpoint"
            )
        if [(m.id, m.type, m.content) for m in migrated.values.get("messages", [])] != [
            (m.id, m.type, m.content) for m in values.get("messages", [])
        ]:
            raise StateSchemaIncompatibleError("迁移消息完整性校验失败，拒绝恢复")
        assert_compatible_state(dict(migrated.values))
        return migrated
    assert_compatible_state(values)
    return snapshot
