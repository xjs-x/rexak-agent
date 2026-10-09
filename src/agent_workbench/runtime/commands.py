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
from agent_workbench.domain.run_state import create_run, find_active_run, get_session
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import CommandRepository
from agent_workbench.persistence.tables import RunRow

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

    repo = CommandRepository(session)
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
        await append_event(
            session,
            session_id=session_id,
            run_id=run_id,
            event_type=EventType.MESSAGE_COMPLETED,
            payload={
                "role": "user",
                "command_id": str(result.row.id),
                "status": "queued",
                "created_run": created_run,
            },
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
