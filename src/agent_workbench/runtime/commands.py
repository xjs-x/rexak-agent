"""用户指令的受理与排队（控制端）。

规则（§4.3）：
- API 收到消息**先写入** `commands`，返回排队/已受理状态（202）；
- **不在** API 线程中执行整个图；
- 无活动 run 时创建新 run；否则排队作为补充消息；
- 同幂等键同内容返回原结果，同键不同内容返回冲突。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import CommandKind, CommandStatus, ErrorCode, RunStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import (
    bump_control_version,
    create_run,
    find_active_run,
    get_session,
    set_run_status,
)
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import CommandRepository
from agent_workbench.persistence.tables import CommandRow, RunRow, SessionRow

#: 这些指令立即提高优先级，绕过普通消息的排队顺序
URGENT_KINDS = frozenset({CommandKind.PAUSE, CommandKind.CANCEL})


@dataclass(slots=True)
class CommandReceipt:
    command_id: UUID
    status: CommandStatus
    created: bool
    run_id: UUID | None
    created_run: bool
    message: str


async def submit_message(
    session: AsyncSession,
    *,
    session_id: UUID,
    content: str,
    idempotency_key: str,
    provider_id: str,
    model_name: str,
    config_snapshot: dict[str, object],
    payload: dict[str, object] | None = None,
) -> CommandReceipt:
    """提交一条用户消息。

    - 无活动 run -> 创建新 run（新阶段）；
    - 有活动 run -> 排队为补充消息，在下一个一致边界注入。
    """
    await get_session(session, session_id)
    await session.execute(select(SessionRow).where(SessionRow.id == session_id).with_for_update())
    repo = CommandRepository(session)
    existing = (
        await session.execute(
            select(CommandRow).where(
                CommandRow.session_id == session_id, CommandRow.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        result = await repo.submit(
            session_id=session_id,
            run_id=existing.run_id,
            kind=CommandKind.MESSAGE,
            content=content,
            idempotency_key=idempotency_key,
            payload=dict(payload or {}),
        )
        return CommandReceipt(
            command_id=result.row.id,
            status=CommandStatus(result.row.status),
            created=False,
            run_id=existing.run_id,
            created_run=False,
            message="已受理过本条输入，返回原指令",
        )

    active = await find_active_run(session, session_id)
    created_run = False
    run_id: UUID | None = active.id if active is not None else None

    if active is None:
        run = await create_run(
            session,
            session_id=session_id,
            objective=content,
            provider_id=provider_id,
            model_name=model_name,
            config_snapshot=config_snapshot,
        )
        run_id = run.id
        created_run = True

    assert run_id is not None
    if active is None:
        # 已结束阶段的未消费指令不应在新的目标下重新执行；原记录保留可查。
        for command in await repo.list_pending(session_id):
            if command.run_id != run_id:
                await repo.mark_status(
                    command.id, CommandStatus.SUPERSEDED, reason="用户已开始新的运行阶段"
                )
    result = await repo.submit(
        session_id=session_id,
        run_id=run_id,
        kind=CommandKind.MESSAGE,
        content=content,
        idempotency_key=idempotency_key,
        payload=dict(payload or {}),
        priority=0,
        control_version=active.control_version if active is not None else 0,
    )

    if result.created:
        if active is not None:
            if not (payload or {}).get("explicit_direction_change"):
                await bump_control_version(session, run_id=active.id)
            if RunStatus(active.status) in {
                RunStatus.PAUSED,
                RunStatus.PAUSING,
                RunStatus.WAITING_USER,
            }:
                active.config_snapshot = {
                    **active.config_snapshot,
                    "restart_checkpoint_on_input": str(result.row.id),
                }
                if RunStatus(active.status) is not RunStatus.PAUSING:
                    await set_run_status(session, run_id=active.id, target=RunStatus.QUEUED)
        from agent_workbench.domain.instruction_scope import sync_instruction_scope

        if not (payload or {}).get("source"):
            await sync_instruction_scope(
                session,
                session_id=session_id,
                run_id=run_id,
                content=content,
                control_changed=active is not None,
            )
        await append_event(
            session,
            session_id=session_id,
            run_id=run_id,
            event_type=EventType.MESSAGE_COMPLETED,
            payload={
                "role": "system" if (payload or {}).get("source") else "user",
                "command_id": str(result.row.id),
                "status": "queued",
                "created_run": created_run,
            },
        )
        # 无委托/暂停期间积压的执行事件：在**下次对话**以聚合摘要注入。
        # 摘要命令优先级低于用户消息，保证先读用户指令再看事件。
        if not (payload or {}).get("source"):
            from agent_workbench.runtime.execution_events import (
                inject_pending_summary_for_new_input,
            )

            try:
                await inject_pending_summary_for_new_input(
                    session, session_id=session_id, run_id=run_id
                )
            except Exception:
                from agent_workbench.observability.logging import get_logger

                get_logger("agent_workbench.commands").warning(
                    "execution_summary_injection_failed", session_id=str(session_id)
                )

    return CommandReceipt(
        command_id=result.row.id,
        status=CommandStatus(result.row.status),
        created=result.created,
        run_id=run_id,
        created_run=created_run,
        message=(
            "已受理：将创建新的运行阶段"
            if created_run
            else "已受理：将在下一个一致边界注入当前运行"
        ),
    )


async def submit_control(
    session: AsyncSession,
    *,
    run: RunRow,
    kind: CommandKind,
    idempotency_key: str,
    payload: dict[str, object] | None = None,
) -> CommandReceipt:
    """提交暂停 / 取消 / 恢复类控制指令。**不虚报"所有工具已停止"。**"""
    priority = 10 if kind in URGENT_KINDS else 5
    repo = CommandRepository(session)
    result = await repo.submit(
        session_id=run.session_id,
        run_id=run.id,
        kind=kind,
        content="",
        idempotency_key=idempotency_key,
        payload=dict(payload or {}),
        priority=priority,
        control_version=run.control_version,
    )

    if result.created:
        await append_event(
            session,
            session_id=run.session_id,
            run_id=run.id,
            event_type=EventType.RUN_STATUS,
            payload={
                "requested": str(kind),
                "command_id": str(result.row.id),
                "note": (
                    "已受理控制请求；实际停止/暂停以工具核对结果为准，"
                    "在核对完成前显示『正在取消/正在暂停』。"
                ),
            },
        )

    return CommandReceipt(
        command_id=result.row.id,
        status=CommandStatus(result.row.status),
        created=result.created,
        run_id=run.id,
        created_run=False,
        message=f"控制请求已受理：{kind}",
    )


async def reject_duplicate_run(session: AsyncSession, session_id: UUID) -> None:
    """防御式：若并发创建了第二个活动 run，按状态给出明确错误。"""
    active = (
        (
            await session.execute(
                select(RunRow).where(
                    RunRow.session_id == session_id,
                    RunRow.status.in_(list(_ACTIVE_LITERALS)),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(active) > 1:
        raise ApiError(
            ErrorCode.RUN_ALREADY_ACTIVE,
            "该 session 存在多个活动 run，违反单一活动运行约束",
            details={"session_id": str(session_id)},
        )


_ACTIVE_LITERALS = tuple(
    str(status)
    for status in (
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_TOOL,
        RunStatus.WAITING_USER,
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.RECOVERING,
    )
)
