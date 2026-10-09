"""Run 状态机、单一活动 run 约束与运行所有权（lease + owner generation）。

三条硬约束：
1. **同一 session 同时最多一个运行所有者**（§4.3）；由数据库部分唯一索引 + 显式事务共同保证；
2. 所有权交接生成**新的 owner generation**；Executor 拒绝已知过期 generation 的新派发；
3. "暂停当前 run 后继续" 与 "完成后开展下一阶段" 是两种操作：
   `resume` 只作用于可恢复的 run；已 completed 的 run 必须用新输入创建下一 run（§4.2）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import (
    ACTIVE_RUN_STATUSES,
    RESUMABLE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    CompletionReason,
    ErrorCode,
    RunStatus,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.tables import RunRow, SessionRow

# --------------------------------------------------------------------------- #
# 合法状态迁移
# --------------------------------------------------------------------------- #

ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    # queued 的 run 还没有任何运行中的工作，取消可以直接落到终态
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_TOOL,
            RunStatus.WAITING_USER,
            RunStatus.PAUSING,
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.RECOVERING,
        }
    ),
    RunStatus.WAITING_TOOL: frozenset(
        {RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.CANCELLING, RunStatus.FAILED}
    ),
    RunStatus.WAITING_USER: frozenset(
        {RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.CANCELLING, RunStatus.FAILED}
    ),
    RunStatus.PAUSING: frozenset({RunStatus.PAUSED, RunStatus.FAILED}),
    RunStatus.PAUSED: frozenset(
        {RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.CANCELLING, RunStatus.FAILED}
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.RECOVERING: frozenset(
        {
            # 核对通过后由恢复流程排队；也允许用户显式确认后走 resume 进入 queued
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
            RunStatus.WAITING_USER,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset({RunStatus.RECOVERING}),
}


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: RunStatus, target: RunStatus) -> None:
    if not can_transition(current, target):
        raise ApiError(
            ErrorCode.CONFLICT,
            f"非法的 run 状态迁移：{current} -> {target}",
            details={"from": str(current), "to": str(target)},
        )


# --------------------------------------------------------------------------- #
# 运行所有权
# --------------------------------------------------------------------------- #


async def claim_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    owner: str,
    lease_seconds: int,
) -> tuple[bool, int]:
    """尝试取得 / 续租 / 接管 run 所有权。

    返回 `(acquired, owner_generation)`。

    规则：
    - 无人持有、或 lease 已过期、或持有者就是自己 -> 可取得；
    - **从别人手里接管（即使 lease 已过期）会递增 generation**，
      这样前任 owner 迟到的响应在派发前检查时会被拒绝（§4.3）；
    - 他人持有且 lease 未过期 -> 拒绝。

    这里用**行锁**而不是条件 UPDATE：`generation` 的递增语义需要读到当前值，
    而行锁在短事务内的代价可以接受（禁止在持锁时等待模型/SSH/浏览器）。
    """
    now = datetime.now(UTC)
    row = (
        await session.execute(select(RunRow).where(RunRow.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")

    if RunStatus(row.status) not in ACTIVE_RUN_STATUSES:
        return False, int(row.owner_generation)

    holder = row.owner
    expired = row.lease_until is None or row.lease_until < now

    if holder is not None and holder != owner and not expired:
        return False, int(row.owner_generation)

    if holder is not None and holder != owner:
        row.owner_generation = int(row.owner_generation) + 1

    row.owner = owner
    row.lease_until = now + timedelta(seconds=lease_seconds)
    await session.flush()
    return True, int(row.owner_generation)


async def release_run(session: AsyncSession, *, run_id: UUID, owner: str) -> None:
    """主动释放所有权。不清零 generation：迟到的旧 owner 仍会因 generation 不匹配被拒绝。"""
    await session.execute(
        update(RunRow)
        .where(RunRow.id == run_id, RunRow.owner == owner)
        .values(owner=None, lease_until=None)
    )
    await session.flush()


async def is_current_owner(
    session: AsyncSession, *, run_id: UUID, owner: str, generation: int
) -> bool:
    """派发前检查：我是否仍是当前 owner 且 generation 未变。

    不能只依赖内存中的"我还是 owner"（§4.3）。
    """
    row = (
        await session.execute(
            select(RunRow.owner, RunRow.owner_generation, RunRow.lease_until).where(
                RunRow.id == run_id
            )
        )
    ).first()
    if row is None:
        return False
    db_owner, db_generation, lease_until = row
    now = datetime.now(UTC)
    if db_owner != owner or int(db_generation) != generation:
        return False
    return lease_until is None or lease_until > now


# --------------------------------------------------------------------------- #
# 状态变更
# --------------------------------------------------------------------------- #


async def set_run_status(
    session: AsyncSession,
    *,
    run_id: UUID,
    target: RunStatus,
    completion_reason: CompletionReason | None = None,
    stop_reason: str | None = None,
) -> RunRow:
    """状态迁移 + 行级锁，保持短事务。

    调用方负责在同一事务内追加对应事件并在提交后广播。
    """
    row = (
        await session.execute(select(RunRow).where(RunRow.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")

    current = RunStatus(row.status)
    if current != target:
        assert_transition(current, target)

    row.status = str(target)
    if completion_reason is not None:
        if target is not RunStatus.COMPLETED:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "completion_reason 只在 run 进入 completed 时设置",
            )
        row.completion_reason = str(completion_reason)
    if stop_reason is not None:
        row.stop_reason = stop_reason
    if target in TERMINAL_RUN_STATUSES or target is RunStatus.PAUSED:
        # 终态与 paused 都释放所有权，避免后续误判"还在跑"
        row.owner = None
        row.lease_until = None
    await session.flush()
    return row


async def bump_control_version(session: AsyncSession, *, run_id: UUID) -> int:
    """用户改变范围或明确打断当前方向时递增。

    旧模型响应可以记录用量，但**派发前必须重新检查版本**（§4.3）。
    """
    row = (
        await session.execute(select(RunRow).where(RunRow.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")
    row.control_version = int(row.control_version) + 1
    await session.flush()
    return int(row.control_version)


# --------------------------------------------------------------------------- #
# 创建 run：同一 session 同时最多一个活动 run
# --------------------------------------------------------------------------- #


async def find_active_run(session: AsyncSession, session_id: UUID) -> RunRow | None:
    return (
        await session.execute(
            select(RunRow)
            .where(
                RunRow.session_id == session_id,
                RunRow.status.in_([s.value for s in ACTIVE_RUN_STATUSES]),
            )
            .order_by(RunRow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_run(
    session: AsyncSession,
    *,
    session_id: UUID,
    objective: str,
    provider_id: str,
    model_name: str,
    config_snapshot: dict[str, object],
    resumed_from_run_id: UUID | None = None,
) -> RunRow:
    """创建新 run。若已存在活动 run 则返回 409。

    依赖 `uq_runs_active_per_session` 部分唯一索引兜底：即使并发提交绕过了
    `find_active_run` 的检查，数据库也会拒绝第二条。
    """
    row = RunRow(
        session_id=session_id,
        objective=objective,
        provider_id=provider_id,
        model_name=model_name,
        status=str(RunStatus.QUEUED),
        config_snapshot=dict(config_snapshot),
        resumed_from_run_id=resumed_from_run_id,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            ErrorCode.RUN_ALREADY_ACTIVE,
            "该 session 已存在活动 run；补充输入请提交消息而非新建 run",
            details={"session_id": str(session_id)},
        ) from exc
    return row


async def assert_resumable(session: AsyncSession, run_id: UUID) -> RunRow:
    """`resume` 只作用于可恢复的 run；completed 的 run 必须创建新 run。"""
    row = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")
    status = RunStatus(row.status)
    if status not in RESUMABLE_RUN_STATUSES:
        if status is RunStatus.COMPLETED:
            raise ApiError(
                ErrorCode.RUN_NOT_RESUMABLE,
                "该 run 已正常结束；请提交新输入以创建下一阶段 run",
                details={"status": str(status)},
            )
        raise ApiError(
            ErrorCode.RUN_NOT_RESUMABLE,
            f"该 run 当前状态不可恢复：{status}",
            details={"status": str(status)},
        )
    return row


async def get_session(session: AsyncSession, session_id: UUID) -> SessionRow:
    row = (
        await session.execute(select(SessionRow).where(SessionRow.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
    return row
