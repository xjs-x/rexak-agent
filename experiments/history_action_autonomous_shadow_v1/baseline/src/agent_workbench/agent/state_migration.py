"""v1–v5 到当前版本的显式兼容适配，使用官方状态更新接口，保留节点边界。

迁移链：1 -> 2 -> 3 -> 4，**不允许跳过**。每次迁移都保留消息 ID、操作 ID、计划与待执行
节点；不把所有旧步骤自动标完成，也不重写历史操作的焦点来美化关联率（§5.4）。
v3→v4 只做计划多线索映射与新字段置空，**不**回写虚构的步骤类型、弱点关系或历史判断。
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


def adapt_v3(
    values: dict[str, Any],
    *,
    checkpoint_id: str,
    next_nodes: tuple[str, ...],
) -> dict[str, Any]:
    """v3 到 v4：计划多线索兼容 + 弱点呈现/复查进度置空。

    - `lead_id` 映射到 `lead_ids`（保留原 `lead_id` 作为兼容投影）；
    - **不**回写虚构的步骤类型与弱点关系：没有依据的步骤 `kind` 保持缺省；
    - 弱点呈现、复查游标与召回去重集合置空：恢复后从领域表重新组装。
    """
    if type(values.get("state_schema_version")) is not int or values["state_schema_version"] != 3:
        raise StateSchemaIncompatibleError("只支持显式 v3 到 v4 适配，拒绝恢复未知状态")
    plan = []
    for step in values.get("plan") or []:
        if not isinstance(step, dict):
            continue
        mapped = dict(step)
        if not mapped.get("lead_ids"):
            primary = mapped.get("lead_id")
            mapped["lead_ids"] = [str(primary)] if primary else []
        plan.append(mapped)
    return {
        "state_schema_version": 4,
        "plan": plan,
        "presented_weakness_ids": [],
        "weakness_review_cursor": None,
        "weakness_review_signature": "",
        "last_weakness_revisions": {},
        "last_recall_keys": [],
        "state_migration": {
            "adapter": "v3_to_v4_plan_leads_and_weakness_progress_v1",
            "from": 3,
            "to": 4,
            "source_checkpoint_id": checkpoint_id,
            "expected_next": list(next_nodes),
        },
    }


def _as_node_candidates(graph: Any, snapshot: Any) -> list[str | None]:
    """迁移写入时 `as_node` 的候选。

    `aupdate_state(as_node=None)` 会让框架自行推断"最后写入状态的节点"，推断结果会改变
    **待执行节点**（`next`）：真实暂停会话停在 `next=('model',)` 时，默认推断会把它算成
    "没有待执行任务"，等于悄悄吞掉一次待执行的模型调用。因此这里枚举候选，
    由调用方逐个尝试并**验证 `next` 未变**后才采用（方案 §10.2）。
    """
    candidates: list[str | None] = [None]
    seen: set[str | None] = {None}

    def push(node: Any) -> None:
        name = str(node)
        # `__end__` 不是可写入的节点；`__start__` 由调用方最后补上。
        if name in seen or name in {"__end__", ""}:
            return
        seen.add(name)
        candidates.append(name)

    # 先试这次 checkpoint 记录过的写入节点（`metadata.writes`），再试图上的其余节点。
    for node in (snapshot.metadata or {}).get("writes") or {}:
        push(node)
    try:
        for node in graph.get_graph().nodes:
            push(node)
    except Exception:  # pragma: no cover - 图对象不可枚举时只退回默认推断
        pass
    push("__start__")
    return candidates


def adapt_v5(
    values: dict[str, Any], *, checkpoint_id: str, next_nodes: tuple[str, ...]
) -> dict[str, Any]:
    """只标记旧完成尚未复查，不猜测完成模式、判断对象或历史依赖。"""
    if type(values.get("state_schema_version")) is not int or values["state_schema_version"] != 5:
        raise StateSchemaIncompatibleError("只支持显式 v5 到 v6 适配")
    plan = [
        {**step, **({"legacy_completion": True} if step.get("status") == "done" else {})}
        for step in values.get("plan") or []
    ]
    assessment = values.get("work_assessment")
    if assessment:
        # v5 在更新后绑定焦点，无法倒推它真正评估了哪一步。
        assessment = {**assessment, "evaluated_step_ids": [], "evaluated_plan_revision": None}
    return {
        "state_schema_version": 6,
        "plan": plan,
        "work_assessment": assessment,
        "last_plan_change": None,
        "state_migration": {
            "adapter": "v5_to_v6_incremental_planning_v1",
            "from": 5,
            "to": 6,
            "source_checkpoint_id": checkpoint_id,
            "expected_next": list(next_nodes),
        },
    }


def adapt_v6(
    values: dict[str, Any], *, checkpoint_id: str, next_nodes: tuple[str, ...]
) -> dict[str, Any]:
    """保留全部历史协议内容，只切换生产语义；不重写消息或旧完成证据。"""
    if type(values.get("state_schema_version")) is not int or values["state_schema_version"] != 6:
        raise StateSchemaIncompatibleError("只支持显式 v6 到 v7 适配")
    return {
        "state_schema_version": 7,
        "state_migration": {
            "adapter": "v6_to_v7_minimal_loop_v1",
            "from": 6,
            "to": 7,
            "source_checkpoint_id": checkpoint_id,
            "expected_next": list(next_nodes),
        },
    }


def adapt_v7(
    values: dict[str, Any], *, checkpoint_id: str, next_nodes: tuple[str, ...]
) -> dict[str, Any]:
    """结束协议退役；原提议仅留历史，不改变消息和待执行边界。"""
    if values.get("state_schema_version") != 7:
        raise StateSchemaIncompatibleError("只支持显式 v7 到 v8 适配")
    return {
        "state_schema_version": 8,
        "state_migration": {
            "adapter": "v7_to_v8_natural_completion_v1",
            "from": 7,
            "to": 8,
            "source_checkpoint_id": checkpoint_id,
            "expected_next": list(next_nodes),
        },
    }


async def _apply_update(
    graph: Any, snapshot: Any, update: dict[str, Any], read_config: dict[str, Any]
) -> Any:
    """迁移写入并**验证待执行节点与消息不变**。

    使用有 checkpoint_id 的配置作原边界更新，不编辑框架序列化表；
    若某个 `as_node` 会改变 `next`，则换成下一个候选重试（每次都从**同一个**原 checkpoint
    出发，因此失败尝试不会成为最终状态）。所有候选都失败才拒绝恢复。

    读取一律落在**本次更新产生的 checkpoint** 上：读 head 会把其它写入的 pending 结果混进来，
    让校验看到不属于这次迁移的状态。
    """
    expected_next = tuple(snapshot.next)
    messages_before = [
        (m.id, m.type, m.content) for m in (snapshot.values or {}).get("messages", [])
    ]
    tried: list[str] = []
    for as_node in _as_node_candidates(graph, snapshot):
        new_config = await graph.aupdate_state(snapshot.config, update, as_node=as_node)
        migrated = await graph.aget_state(new_config or read_config)
        messages_after = [
            (m.id, m.type, m.content) for m in (migrated.values or {}).get("messages", [])
        ]
        if tuple(migrated.next) == expected_next and messages_after == messages_before:
            return migrated
        tried.append(f"{as_node!r}->next={tuple(migrated.next)}")
    raise StateSchemaIncompatibleError(
        "迁移无法保持待执行节点与消息不变，拒绝恢复；需核对原 checkpoint。"
        f"已尝试：{tried}。请先备份并显式核对 pending 任务后再恢复。"
    )


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
        if any(
            getattr(task, "result", None) is not None
            for task in (getattr(snapshot, "tasks", None) or ())
        ):
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
        if any(
            getattr(task, "result", None) is not None
            for task in (getattr(snapshot, "tasks", None) or ())
        ):
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
        version = values.get("state_schema_version")

    if version == 3:
        if any(
            getattr(task, "result", None) is not None
            for task in (getattr(snapshot, "tasks", None) or ())
        ):
            raise StateSchemaIncompatibleError(
                "v3 checkpoint 含未提交的并行结果，拒绝迁移；需先核对操作台账"
            )
        source_id = str(snapshot.config["configurable"]["checkpoint_id"])
        migrated = await _apply_update(
            graph,
            snapshot,
            adapt_v3(values, checkpoint_id=source_id, next_nodes=tuple(snapshot.next)),
            config,
        )
        _assert_migration_preserved(
            {"next": snapshot.next, "messages": values.get("messages", [])}, migrated
        )
        snapshot = migrated
        values = dict(migrated.values or {})

    if values.get("state_schema_version") == 4:
        if any(
            getattr(task, "result", None) is not None
            for task in (getattr(snapshot, "tasks", None) or ())
        ):
            raise StateSchemaIncompatibleError(
                "v4 checkpoint 含未提交的并行结果，拒绝迁移；需先核对台账"
            )
        migrated = await _apply_update(
            graph,
            snapshot,
            {
                "state_schema_version": 5,
                "active_skills": {},
                "skill_directives": {},
                "state_migration": {
                    "adapter": "v4_to_v5_skills_v1",
                    "from": 4,
                    "to": 5,
                    "source_checkpoint_id": str(snapshot.config["configurable"]["checkpoint_id"]),
                    "expected_next": list(snapshot.next),
                },
            },
            config,
        )
        _assert_migration_preserved(
            {"next": snapshot.next, "messages": values.get("messages", [])}, migrated
        )
        snapshot = migrated
        values = dict(migrated.values or {})
    if values.get("state_schema_version") == 5:
        if any(
            getattr(task, "result", None) is not None
            for task in (getattr(snapshot, "tasks", None) or ())
        ):
            raise StateSchemaIncompatibleError(
                "v5 checkpoint 含未提交并行结果，拒绝迁移；需先核对操作台账"
            )
        migrated = await _apply_update(
            graph,
            snapshot,
            adapt_v5(
                values,
                checkpoint_id=str(snapshot.config["configurable"]["checkpoint_id"]),
                next_nodes=tuple(snapshot.next),
            ),
            config,
        )
        _assert_migration_preserved(
            {"next": snapshot.next, "messages": values.get("messages", [])}, migrated
        )
        snapshot = migrated
        values = dict(migrated.values or {})
    if values.get("state_schema_version") == 6:
        if any(
            getattr(task, "result", None) is not None
            for task in (getattr(snapshot, "tasks", None) or ())
        ):
            raise StateSchemaIncompatibleError(
                "v6 checkpoint 含未提交并行结果，拒绝迁移；需先核对操作台账"
            )
        migrated = await _apply_update(
            graph,
            snapshot,
            adapt_v6(
                values,
                checkpoint_id=str(snapshot.config["configurable"]["checkpoint_id"]),
                next_nodes=tuple(snapshot.next),
            ),
            config,
        )
        _assert_migration_preserved(
            {"next": snapshot.next, "messages": values.get("messages", [])}, migrated
        )
        snapshot = migrated
        values = dict(migrated.values or {})
    if values.get("state_schema_version") == 7:
        if any(getattr(task, "result", None) is not None for task in (snapshot.tasks or ())):
            raise StateSchemaIncompatibleError(
                "v7 checkpoint 含未提交并行结果，拒绝迁移；需核对台账"
            )
        migrated = await _apply_update(
            graph,
            snapshot,
            adapt_v7(
                values,
                checkpoint_id=str(snapshot.config["configurable"]["checkpoint_id"]),
                next_nodes=tuple(snapshot.next),
            ),
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
    "adapt_v3",
    "adapt_v5",
    "adapt_v6",
    "ensure_current_checkpoint",
    "needs_migration",
]
