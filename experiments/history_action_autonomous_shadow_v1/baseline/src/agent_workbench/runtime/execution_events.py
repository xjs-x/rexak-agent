"""执行事件拉取、去重、wait-set 评估与值班委托触发（Runner 基础设施，不消耗模型调用）。

契约（规格 §10、§11）：
- 轮询/订阅属于 Runner 职责；以 `(executor_id, event_id)` 幂等入库，
  **同批事件落库成功后才推进远端读取游标**；
- 无活动 run 的保留资源也持续同步；网络不通维持最后核对时间与 stale/unknown，
  不自动重做资源操作；
- 无委托或暂停期间**不自动调用模型**：事件被记录并在下次对话以聚合摘要注入；
- 有效委托命中：当前 run 在推进时并入一致边界（MESSAGE 指令），
  正在等待该条件时重新排队；关联 run 已终态时用既有领域路径建立同 thread 后续 run；
- 取消委托递增 revision；旧 revision 不再触发新副作用，资源不被关闭。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import CommandKind, RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import find_active_run
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
    HttpExecutorClient,
)
from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ExecutionDelegationRepository,
    ExecutionDeliveryBatchRepository,
    ExecutionEventCursorRepository,
    ExecutionEventReceiptRepository,
    ExecutionResourceRepository,
    ExecutionWaitSetRepository,
)
from agent_workbench.persistence.tables import (
    ExecutionDelegationRow,
    ExecutionDeliveryBatchRow,
    ExecutionEventReceiptRow,
    ExecutionResourceRow,
    ExecutionWaitSetRow,
    RunRow,
    SessionRow,
    ToolOperationRow,
)

log = get_logger("agent_workbench.execution_events")

#: 单次拉取的事件页大小与单批聚合上限。
EVENT_FETCH_LIMIT = 200
DELIVERY_EVENT_LIMIT = 50
#: 事件类型 -> 中文短标签（确定性聚合，不调用模型）
_EVENT_LABELS = {
    "resource.received": "受理",
    "resource.started": "启动",
    "resource.exited": "退出",
    "resource.closed": "关闭",
    "resource.failed": "失败",
    "resource.interaction_lost": "交互丢失",
    "resource.watch_match": "监视命中",
    "resource.cancel_requested": "取消请求",
    "resource.cancel_confirmed": "取消确认",
    "output.limit_reached": "日志超限",
    "output.dropped": "日志丢弃",
    "resource.warning": "资源告警",
    "resource.recovery_checked": "恢复核对",
    "terminal.dispatch_notified": "首次等待通知",
    "control.synced": "控制同步",
}
_TERMINAL_RESOURCE_STATES = {"exited", "closed"}


def _needs_resource_refresh():
    return or_(
        ExecutionResourceRow.state.in_(["starting", "running", "unknown"]),
        select(ToolOperationRow.operation_id).where(
            ToolOperationRow.operation_id == ExecutionResourceRow.origin_operation_id,
            ToolOperationRow.status.in_(["accepted", "running", "unknown"]),
        ).exists(),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def sessions_with_execution_activity(db: Database) -> list[UUID]:
    """有执行活动（活动资源 / 委托 / 未满足 wait-set / 未交付事件）的会话。"""
    session_ids: set[UUID] = set()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(ExecutionResourceRow.session_id).where(
                    _needs_resource_refresh()
                )
            )
        ).all()
        session_ids.update(item[0] for item in rows)
        rows = (
            await session.execute(
                select(ExecutionDelegationRow.session_id).where(
                    ExecutionDelegationRow.status == "active"
                )
            )
        ).all()
        session_ids.update(item[0] for item in rows)
        rows = (
            await session.execute(
                select(ExecutionWaitSetRow.session_id).where(ExecutionWaitSetRow.status == "open")
            )
        ).all()
        session_ids.update(item[0] for item in rows)
        rows = (
            await session.execute(
                select(ExecutionDeliveryBatchRow.session_id).where(
                    ExecutionDeliveryBatchRow.status.in_(["prepared", "injected"])
                )
            )
        ).all()
        session_ids.update(item[0] for item in rows)
    return sorted(session_ids, key=str)


async def pull_events(db: Database, *, settings: Settings, client: Any) -> dict[str, Any]:
    """拉取远端持久事件到控制端投影。游标只在同批落库成功后推进。"""
    result: dict[str, Any] = {"pulled": 0, "created": 0, "notes": []}
    async with db.session() as session:
        cursor = await ExecutionEventCursorRepository(session).get(settings.executor_id)
    try:
        page = await client.execution_events(after=cursor, limit=EVENT_FETCH_LIMIT)
    except (ExecutorConnectionError, ExecutorProtocolError) as exc:
        result["notes"].append(f"读取执行事件失败（保持现有游标）：{type(exc).__name__}")
        return result
    events = list(page.get("events") or [])
    if events:
        async with db.transaction() as session:
            receipts = ExecutionEventReceiptRepository(session)
            created = 0
            max_seq = cursor
            for item in events:
                session_id = _session_uuid(item.get("session_id"))
                _row, is_new = await receipts.insert_if_new(
                    executor_id=str(item.get("executor_id") or settings.executor_id),
                    event_id=str(item.get("event_id")),
                    session_id=session_id,
                    resource_id=_optional_str(item.get("resource_id")),
                    operation_id=_optional_str(item.get("operation_id")),
                    type=str(item.get("type")),
                    occurred_at=_parse_time(item.get("occurred_at")),
                    sequence=int(item.get("seq") or 0),
                    availability=str(item.get("availability") or "available"),
                    payload=dict(item.get("payload") or {}),
                )
                created += 1 if is_new else 0
                max_seq = max(max_seq, int(item.get("seq") or 0))
            # 同一事务内推进游标：不会越过未提交数据。
            await ExecutionEventCursorRepository(session).put(settings.executor_id, max_seq)
        result["pulled"] = len(events)
        result["created"] = created
    return result


async def refresh_resources(db: Database, *, settings: Settings, client: Any) -> None:
    """核对控制端已知活跃资源；事件接收本身不能替代状态同步。"""
    async with db.session() as session:
        rows = (await session.execute(select(ExecutionResourceRow).where(
            _needs_resource_refresh()
        ).limit(1000))).scalars().all()
    for row in rows:
        try:
            view = await client.execution_resource(str(row.resource_id))
        except (ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected):
            continue
        async with db.transaction() as session:
            await ExecutionResourceRepository(session).upsert(
                resource_id=row.resource_id, session_id=row.session_id,
                executor_id=settings.executor_id, kind=str(view.get("kind") or row.kind),
                state=str(view.get("state") or "unknown"),
                interaction_state=str(view.get("interaction_state") or row.interaction_state),
                exit_code=view.get("exit_code"), termination_reason=view.get("termination_reason"),
                output_limit_action=str(view.get("output_limit_action") or row.output_limit_action),
                quota={key: view.get(key) for key in (
                    "output_bytes", "dropped_bytes", "output_limit_bytes"
                )},
            )


async def confirm_deliveries(db: Database) -> list[dict[str, Any]]:
    """只以真实 checkpoint 回执确认交付，指令 consumed 不是图写盘证明。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.persistence.bootstrap import open_checkpointer_connection

    confirmed: list[dict[str, Any]] = []
    async with db.session() as session:
        candidates = (await session.execute(
            select(ExecutionDeliveryBatchRow, SessionRow.thread_id)
            .join(SessionRow, SessionRow.id == ExecutionDeliveryBatchRow.session_id)
            .where(ExecutionDeliveryBatchRow.status == "injected")
        )).all()
    committed: list[UUID] = []
    if candidates:
        try:
            async with open_checkpointer_connection(get_settings()) as connection:
                saver = AsyncPostgresSaver(connection)
                for batch, thread_id in candidates:
                    saved = await saver.aget_tuple({"configurable": {"thread_id": str(thread_id)}})
                    values = saved.checkpoint.get("channel_values", {}) if saved else {}
                    if values.get("execution_delivery_batch_id") == str(batch.id):
                        committed.append(batch.id)
        except Exception as exc:
            log.warning("execution_delivery_checkpoint_unavailable", error_type=type(exc).__name__)
            return confirmed
    async with db.transaction() as session:
        repository = ExecutionDeliveryBatchRepository(session)
        delegation_repo = ExecutionDelegationRepository(session)
        for batch_id in committed:
            batch = await repository.get(batch_id)
            if batch is None or batch.status != "injected":
                continue
            await repository.mark_delivered(batch.id)
            if batch.delegation_id is not None:
                delegation = await delegation_repo.get(batch.delegation_id)
                if delegation is not None and delegation.status == "active":
                    await delegation_repo.touch_triggered(batch.delegation_id)
            confirmed.append({"batch_id": str(batch.id), "session_id": str(batch.session_id)})
        finished_once = (await session.execute(
            select(ExecutionDeliveryBatchRow.delegation_id)
            .join(RunRow, RunRow.id == ExecutionDeliveryBatchRow.run_id)
            .where(
                ExecutionDeliveryBatchRow.status == "delivered",
                RunRow.status.in_(["completed", "failed", "cancelled"]),
            )
        )).scalars().all()
        for delegation_id in set(finished_once):
            if delegation_id is not None:
                row = await delegation_repo.get(delegation_id)
                if row is not None and row.status == "active" and row.mode == "once":
                    await delegation_repo.complete_once(delegation_id)
    return confirmed


async def evaluate_wait_sets(
    db: Database, *, settings: Settings, client: Any, run_ids: list[UUID] | None = None
) -> dict[str, Any]:
    """核对未满足的 wait-set：按 any/all 判定，满足时标记并供唤醒使用。"""
    result: dict[str, Any] = {"checked": 0, "satisfied": [], "notes": []}
    async with db.session() as session:
        stmt = select(ExecutionWaitSetRow).where(ExecutionWaitSetRow.status == "open")
        if run_ids is not None:
            stmt = stmt.where(ExecutionWaitSetRow.run_id.in_(run_ids))
        wait_sets = list((await session.execute(stmt)).scalars().all())
    for wait_set in wait_sets:
        result["checked"] += 1
        conditions: list[dict[str, Any]] = []
        for resource_id in wait_set.resource_ids or []:
            state = None
            view: dict[str, Any] = {}
            interaction = ""
            try:
                view = await client.execution_resource(str(resource_id))
                state = str(view.get("state") or "unknown")
                interaction = str(view.get("interaction_state") or "")
            except ExecutorConnectionError:
                result["notes"].append("执行岛不可达：wait-set 保持未满足，不自动重做")
                state = None
            except ExecutorProtocolError:
                state = "unknown"
                interaction = ""
            if state is None:
                conditions.append({"resource_id": resource_id, "state": "unreachable", "ok": False})
                continue
            async with db.transaction() as session:
                await ExecutionResourceRepository(session).upsert(
                    resource_id=UUID(str(resource_id)),
                    session_id=wait_set.session_id,
                    executor_id=settings.executor_id,
                    kind=str(view.get("kind") or "command"),
                    state=state,
                    interaction_state=interaction or "not_applicable",
                    exit_code=view.get("exit_code"),
                    termination_reason=view.get("termination_reason"),
                )
            conditions.append(
                {
                    "resource_id": resource_id,
                    "state": state,
                    "ok": state in _TERMINAL_RESOURCE_STATES,
                }
            )
        # 事件条件：wait-set 创建之后、类型匹配的已接收事件
        event_hit: dict[str, Any] | None = None
        if wait_set.event_types:
            async with db.session() as session:
                receipts = (
                    (
                        await session.execute(
                            select(ExecutionEventReceiptRow)
                            .where(
                                ExecutionEventReceiptRow.session_id == wait_set.session_id,
                                ExecutionEventReceiptRow.type.in_(list(wait_set.event_types)),
                                ExecutionEventReceiptRow.received_at >= wait_set.created_at,
                                *([ExecutionEventReceiptRow.resource_id.in_(
                                    list(wait_set.resource_ids)
                                )] if wait_set.resource_ids else []),
                            )
                            .order_by(ExecutionEventReceiptRow.id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .all()
                )
            if receipts:
                event_hit = {"type": receipts[0].type, "resource_id": receipts[0].resource_id}
        satisfied = False
        detail: dict[str, Any] = {"conditions": conditions}
        flags = [bool(item["ok"]) for item in conditions]
        if wait_set.event_types:
            flags.append(event_hit is not None)
        satisfied = bool(flags) and (all(flags) if wait_set.match == "all" else any(flags))
        if event_hit:
            detail["event"] = event_hit
        if satisfied:
            async with db.transaction() as session:
                await ExecutionWaitSetRepository(session).mark_satisfied(
                    wait_set.id, detail=detail
                )
            result["satisfied"].append(
                {"wait_set_id": str(wait_set.id), "run_id": str(wait_set.run_id)}
            )
    # 先前满足时 run 可能仍在执行；直到实际进入 waiting_tool 再完成一次唤醒。
    from agent_workbench.runtime.jobs import _wake_run

    async with db.session() as session:
        satisfied_stmt = select(ExecutionWaitSetRow).where(
            ExecutionWaitSetRow.status == "satisfied"
        )
        if run_ids is not None:
            satisfied_stmt = satisfied_stmt.where(ExecutionWaitSetRow.run_id.in_(run_ids))
        satisfied_sets = (await session.execute(satisfied_stmt)).scalars().all()
    for item in satisfied_sets:
        if item.run_id and await _wake_run(
            db, item.run_id, summary="指定的执行等待条件已满足，请核对相关结果。",
            terminal_job_ids=[],
            explicit_wait_satisfied=True,
        ):
            async with db.transaction() as session:
                current = await session.get(ExecutionWaitSetRow, item.id)
                if current is not None:
                    current.status = "notified"
    return result


async def trigger_delegations(
    db: Database, *, settings: Settings, client: Any
) -> dict[str, Any]:
    """有效委托命中事件时的处置与聚合注入。"""
    # `client` 由调用方持有（同一周期内的连接复用）；本函数只访问控制端数据库。
    _ = client
    result: dict[str, Any] = {"sessions": [], "injected": [], "notes": []}
    session_ids = await sessions_with_execution_activity(db)
    for session_id in session_ids:
        async with db.session() as session:
            delegations = list(
                await ExecutionDelegationRepository(session).active_for_session(session_id)
            )
            if not delegations:
                continue
            unconsumed = list(
                await ExecutionEventReceiptRepository(session).list_unconsumed(
                    session_id, limit=200
                )
            )
        if not unconsumed:
            continue
        matches = _match_delegations(delegations, unconsumed)
        if not matches:
            continue
        result["sessions"].append(str(session_id))
        for delegation, receipts in matches:
            outcome = await _deliver_for_delegation(
                db,
                settings=settings,
                session_id=session_id,
                delegation=delegation,
                receipts=receipts,
            )
            if outcome is not None:
                result["injected"].append(outcome)
    return result


def _match_delegations(
    delegations: list[ExecutionDelegationRow], receipts: list[Any]
) -> list[tuple[ExecutionDelegationRow, list[Any]]]:
    from agent_workbench.domain.execution_delegations import matches_event

    out: list[tuple[ExecutionDelegationRow, list[Any]]] = []
    for delegation in delegations:
        if delegation.mode == "once" and delegation.triggered_count:
            continue
        matched = [
            item
            for item in receipts
            if matches_event(
                delegation,
                {
                    "resource_id": item.resource_id,
                    "type": item.type,
                },
            )
        ]
        if matched:
            out.append((delegation, matched))
    return out


async def _deliver_for_delegation(
    db: Database,
    *,
    settings: Settings,
    session_id: UUID,
    delegation: ExecutionDelegationRow,
    receipts: list[Any],
) -> dict[str, Any] | None:
    # 同一会话的接收、暂停检查、建批次和入队在一个事务内完成。
    async with db.transaction() as session:
        session_row = (await session.execute(
            select(SessionRow).where(SessionRow.id == session_id).with_for_update()
        )).scalar_one_or_none()
        if session_row is None or session_row.status == "deleted":
            return None
        current = await ExecutionDelegationRepository(session).get(delegation.id)
        if current is None or current.status != "active" or current.revision != delegation.revision:
            return None
        active = await find_active_run(session, session_id)
        if active is not None and RunStatus(active.status) in {
            RunStatus.PAUSED, RunStatus.PAUSING, RunStatus.WAITING_USER,
            RunStatus.CANCELLING, RunStatus.RECOVERING,
        }:
            return None
        open_batches = list(
            await ExecutionDeliveryBatchRepository(session).list_open_for_session(session_id)
        )
        if open_batches:
            return None
        available = await ExecutionEventReceiptRepository(session).list_unconsumed(
            session_id, limit=EVENT_FETCH_LIMIT
        )
        wanted = {int(item.id) for item in receipts}
        included = [item for item in available if int(item.id) in wanted][:DELIVERY_EVENT_LIMIT]
        if not included:
            return None
        summary = summarize_events(included, delegation=delegation)
        batch_id = uuid4()
        batch = await ExecutionDeliveryBatchRepository(session).create(
            session_id=session_id,
            run_id=active.id if active else None,
            delegation_id=delegation.id,
            event_receipt_ids=[int(item.id) for item in included],
            summary=summary,
            message_id=f"exec-delivery-{batch_id}",
            batch_id=batch_id,
        )
        text = render_summary_text(summary, delegation=delegation)
        if active is not None and RunStatus(active.status) in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.WAITING_TOOL,
            RunStatus.RECOVERING,
        }:
            command = await CommandRepository(session).submit(
                session_id=session_id,
                run_id=active.id,
                kind=CommandKind.MESSAGE,
                content=text,
                idempotency_key=batch.message_id,
                payload={
                    "source": "execution_duty",
                    "batch_id": str(batch.id),
                    "delegation_id": str(delegation.id),
                    "delegation_revision": delegation.revision,
                },
            )
            if RunStatus(active.status) is RunStatus.WAITING_TOOL:
                from agent_workbench.domain.run_state import set_run_status

                await set_run_status(session, run_id=active.id, target=RunStatus.QUEUED)
            await ExecutionDeliveryBatchRepository(session).mark_injected(
                batch.id, command_id=command.row.id
            )
            await ExecutionEventReceiptRepository(session).mark_consumed(
                [int(item.id) for item in included], batch.id
            )
            await append_event(
                session,
                session_id=session_id,
                run_id=active.id,
                event_type=EventType.MESSAGE_COMPLETED,
                payload={
                    "role": "system",
                    "scope": "execution_duty",
                    "batch_id": str(batch.id),
                    "delegation_id": str(delegation.id),
                    "events": len(included),
                },
            )
            return {
                "batch_id": str(batch.id),
                "run_id": str(active.id),
                "mode": "injected_into_active_run",
                "delegation_id": str(delegation.id),
            }
        # 没有活动 run：用既有领域路径建立同 session/thread 的后续 run。
        from agent_workbench.runtime.commands import submit_message
        session_row = (
            await session.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if session_row is None or session_row.status == "deleted":
            return None
        provider_id = session_row.provider_id or settings.default_provider
        model_name = session_row.model_name or settings.model_for_provider(provider_id)
        receipt = await submit_message(
            session,
            session_id=session_id,
            content=text,
            idempotency_key=batch.message_id,
            provider_id=provider_id,
            model_name=model_name,
            config_snapshot={
                "created_from": "execution_duty",
                "delegation_id": str(delegation.id),
            },
            payload={
                "source": "execution_duty",
                "batch_id": str(batch.id),
                "delegation_id": str(delegation.id),
                "delegation_revision": delegation.revision,
            },
        )
        batch.run_id = receipt.run_id
        await ExecutionDeliveryBatchRepository(session).mark_injected(
            batch.id, command_id=receipt.command_id
        )
        await ExecutionEventReceiptRepository(session).mark_consumed(
            [int(item.id) for item in included], batch.id
        )
        return {
            "batch_id": str(batch.id),
            "run_id": str(receipt.run_id),
            "mode": "created_run" if receipt.created_run else "queued_into_run",
            "delegation_id": str(delegation.id),
        }


def summarize_events(
    receipts: list[Any], *, delegation: ExecutionDelegationRow | None = None
) -> dict[str, Any]:
    """确定性聚合摘要：资源用途、事件类型、首次/最后时间、次数、重要结果、缺失与引用。

    不调用模型；预算有界；未装入摘要的事件保持 pending。
    """
    by_type: dict[str, int] = {}
    by_resource: dict[str, dict[str, Any]] = {}
    important: list[str] = []
    first_at: str | None = None
    last_at: str | None = None
    missing_notes: list[str] = []
    for item in receipts:
        by_type[item.type] = by_type.get(item.type, 0) + 1
        key = str(item.resource_id or "")
        bucket = by_resource.setdefault(
            key, {"resource_id": key, "events": 0, "types": set(), "purposes": set()}
        )
        bucket["events"] += 1
        bucket["types"].add(item.type)
        purpose = (item.payload or {}).get("purpose")
        if purpose:
            bucket["purposes"].add(str(purpose))
        occurred = item.occurred_at.isoformat() if item.occurred_at else ""
        if occurred and (first_at is None or occurred < first_at):
            first_at = occurred
        if occurred and (last_at is None or occurred > last_at):
            last_at = occurred
        payload = item.payload or {}
        if item.type == "resource.exited":
            important.append(
                f"{key[:8]} 退出（exit={payload.get('exit_code')}，"
                f"原因={payload.get('termination_reason')}）"
            )
        elif item.type == "resource.watch_match":
            important.append(f"{key[:8]} 监视命中：{str(payload.get('pattern'))[:80]}")
        elif item.type == "output.limit_reached":
            important.append(
                f"{key[:8]} 日志超限（{payload.get('action')}，丢弃 {payload.get('dropped_bytes')} 字节）"
            )
        elif item.type == "resource.interaction_lost":
            important.append(f"{key[:8]} 交互丢失：{str(payload.get('note'))[:80]}")
        elif item.type == "resource.failed":
            important.append(f"{key[:8]} 失败：{str(payload.get('message'))[:80]}")
        if item.availability != "available":
            missing_notes.append(f"{key[:8]} 证据可用性：{item.availability}")
    resources = [
        {
            "resource_id": bucket["resource_id"],
            "events": bucket["events"],
            "types": sorted(bucket["types"]),
            "purposes": sorted(bucket["purposes"]),
        }
        for bucket in by_resource.values()
    ]
    return {
        "event_count": len(receipts),
        "first_occurred_at": first_at,
        "last_occurred_at": last_at,
        "by_type": {key: by_type[key] for key in sorted(by_type)},
        "by_type_labels": {
            key: _EVENT_LABELS.get(key, key) for key in sorted(by_type)
        },
        "resources": resources,
        "important": important[:20],
        "availability_notes": missing_notes[:20],
        "truncated": len(receipts) >= DELIVERY_EVENT_LIMIT,
        "delegation_id": str(delegation.id) if delegation is not None else None,
        "delegation_mode": delegation.mode if delegation is not None else None,
        "delegation_goal": delegation.goal if delegation is not None else "",
        "watermark": {
            "receipt_ids": [int(item.id) for item in receipts],
        },
    }


def render_summary_text(
    summary: dict[str, Any], *, delegation: ExecutionDelegationRow | None = None
) -> str:
    """注入模型上下文的事件聚合文本（系统通知，与用户消息区分）。"""
    lines = ["【系统通知·执行事件聚合】"]
    if delegation is not None:
        mode = "持续值班" if delegation.mode == "continuous" else "一次性值班"
        lines.append(f"委托：{mode}｜目标：{delegation.goal or '（未填写）'}")
    lines.append(
        f"事件 {summary['event_count']} 条"
        f"（{summary.get('first_occurred_at') or '未知'} → {summary.get('last_occurred_at') or '未知'}）"
    )
    labels = summary.get("by_type_labels") or {}
    lines.append("按类型：" + "；".join(f"{key}×{value}" for key, value in labels.items()))
    for item in summary.get("resources") or []:
        purposes = "、".join(item.get("purposes") or []) or "（未说明用途）"
        lines.append(f"- 资源 {str(item.get('resource_id'))[:8]}：{item.get('events')} 条 / {purposes}")
    if summary.get("important"):
        lines.append("重要结果：")
        lines.extend(f"- {item}" for item in summary["important"])
    if summary.get("availability_notes"):
        lines.append("⚠ 证据可用性：" + "；".join(summary["availability_notes"]))
    if summary.get("truncated"):
        lines.append(
            "本次摘要只装入预算内的事件；其余事件保持待交付，不会被标为已获知。"
        )
    lines.append(
        "请判断这些事件是否影响当前目标；如需读取输出，使用资源/输出工具按游标读取。"
        "事件通知不代表程序执行成功或任务目标达成。"
    )
    return "\n".join(lines)


async def inject_pending_summary_for_new_input(
    session: Any, *, session_id: UUID, run_id: UUID | None
) -> dict[str, Any] | None:
    """新用户输入时把积压的执行事件以聚合摘要注入（在用户消息之后消费）。

    契约（规格 §11.3 / F08）：
    - UI 已读不消费模型交付位置；摘要仍必须注入；
    - 未装入本次摘要的事件保持 pending，不会被标为已获知；
    - 用稳定 message_id + 幂等键注入，重复提交不会产生第二条摘要。
    """
    open_batches = list(
        await ExecutionDeliveryBatchRepository(session).list_open_for_session(session_id)
    )
    if open_batches:
        return None
    unconsumed = list(
        await ExecutionEventReceiptRepository(session).list_unconsumed(
            session_id, limit=DELIVERY_EVENT_LIMIT
        )
    )
    if not unconsumed:
        return None
    summary = summarize_events(unconsumed)
    batch_id = uuid4()
    batch = await ExecutionDeliveryBatchRepository(session).create(
        session_id=session_id,
        run_id=run_id,
        delegation_id=None,
        event_receipt_ids=[int(item.id) for item in unconsumed],
        summary=summary,
        message_id=f"exec-delivery-{batch_id}",
        batch_id=batch_id,
    )
    command = await CommandRepository(session).submit(
        session_id=session_id,
        run_id=run_id,
        kind=CommandKind.MESSAGE,
        content=render_summary_text(summary),
        idempotency_key=batch.message_id,
        payload={"source": "execution_events", "batch_id": str(batch.id)},
        # 低于用户消息：先消耗用户指令，再在同一 run 里消费事件聚合。
        priority=-1,
    )
    await ExecutionDeliveryBatchRepository(session).mark_injected(
        batch.id, command_id=command.row.id
    )
    await ExecutionEventReceiptRepository(session).mark_consumed(
        [int(item.id) for item in unconsumed], batch.id
    )
    return {"batch_id": str(batch.id), "events": len(unconsumed), "command_id": str(command.row.id)}


async def execution_cycle(db: Database, *, settings: Settings) -> dict[str, Any]:
    """Runner 一个周期：事件拉取 -> 等待评估 -> 委托触发 -> delivery 确认。

    没有执行活动时只是一次廉价查询，不会触碰执行岛。
    """
    summary: dict[str, Any] = {
        "active_sessions": 0,
        "pulled": 0,
        "wait_satisfied": [],
        "injected": [],
        "deliveries": [],
        "notes": [],
    }
    try:
        session_ids = await sessions_with_execution_activity(db)
    except Exception as exc:  # 数据库故障不应让调度循环崩溃
        summary["notes"].append(f"执行活动扫描失败：{type(exc).__name__}")
        return summary
    confirmed = await confirm_deliveries(db)
    summary["deliveries"] = confirmed
    if not session_ids:
        return summary
    summary["active_sessions"] = len(session_ids)

    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(tunnel, settings=settings, expected_executor_id=settings.executor_id)
    try:
        await client.start()
    except (TunnelUnavailable, ExecutorConnectionError) as exc:
        summary["notes"].append(
            f"执行岛不可达，本轮不拉取事件（保留最后核对时间，不自动重做）："
            f"{type(exc).__name__}"
        )
        await _close(client, tunnel)
        return summary
    try:
        pulled = await pull_events(db, settings=settings, client=client)
        summary["pulled"] = pulled.get("pulled", 0)
        summary["notes"].extend(pulled.get("notes", []))
        await refresh_resources(db, settings=settings, client=client)
        evaluated = await evaluate_wait_sets(db, settings=settings, client=client)
        summary["wait_satisfied"] = evaluated.get("satisfied", [])
        summary["notes"].extend(evaluated.get("notes", []))
        triggered = await trigger_delegations(db, settings=settings, client=client)
        summary["injected"] = triggered.get("injected", [])
        summary["notes"].extend(triggered.get("notes", []))
    except (ExecutorConnectionError, ExecutorProtocolError) as exc:
        summary["notes"].append(f"执行事件周期失败：{type(exc).__name__}")
    finally:
        await _close(client, tunnel)
    return summary


async def _close(client: Any, tunnel: Any) -> None:
    for item in (client, tunnel):
        close = getattr(item, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            continue


def _session_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    return None if value in {None, ""} else str(value)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return _utcnow()
