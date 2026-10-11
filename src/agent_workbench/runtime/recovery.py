"""恢复：checkpoint 与远端任务核对。

规则（§6.4、§8.4、ACCEPTANCE §3）：
- 框架 graph checkpoint **不包含远端进程**；恢复前**必须**核对 job 状态；
- 重启后原任务可能仍在运行、已退出或无法核实 —— **分别核对**，
  **不统一标为失败后自动重跑**；
- 恢复现有 interrupt 用其规定的 resume 方式；已完成阶段的新消息用普通输入继续同一 thread；
- 无法确定时明确标 `unknown` 并保留证据与处置入口，**禁止伪造成功或盲目再执行**。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import set_run_status
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
    HttpExecutorClient,
)
from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.persistence.db import Database, get_database
from agent_workbench.persistence.repositories import ToolJobRepository
from agent_workbench.persistence.tables import ArtifactRow, RunRow

log = get_logger("agent_workbench.recovery")


@dataclass(slots=True)
class ReconciliationReport:
    """恢复核对结果。每一项都要能说明"为什么这样判定"。"""

    runs_requeued: list[str] = field(default_factory=list)
    #: 核对通过、允许继续的 run
    allowed_to_resume: list[str] = field(default_factory=list)
    #: 因远端状态无法核实而保持 recovering、等待用户显式确认的 run
    blocked_pending_user: list[str] = field(default_factory=list)
    jobs_reconciled: list[dict[str, str]] = field(default_factory=list)
    jobs_unverifiable: list[str] = field(default_factory=list)
    orphan_artifacts_flagged: list[str] = field(default_factory=list)
    executor_reachable: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs_marked_recovering": self.runs_requeued,
            "allowed_to_resume": self.allowed_to_resume,
            "blocked_pending_user": self.blocked_pending_user,
            "jobs_reconciled": self.jobs_reconciled,
            "jobs_unverifiable": self.jobs_unverifiable,
            "orphan_artifacts_flagged": self.orphan_artifacts_flagged,
            "executor_reachable": self.executor_reachable,
            "notes": self.notes,
        }


async def recover_stale_runs(
    db: Database,
    settings: Settings | None = None,
    *,
    only_run_id: UUID | None = None,
    owner_id: str | None = None,
) -> list[UUID]:
    """把 lease 已过期的 `running` run 置为 `recovering` 并**重新排队**。

    不直接跑：先让核对流程检查远端副作用，再决定继续还是等待用户。

    判定口径是**"lease 是否真的过期"**：`lease_until < now`。
    早先实现用 `now - run_lease_seconds` 当截止，等价于要求"过期之后又过了一个完整
    lease 周期才算 stale"，与文档承诺的"lease 过期即恢复"不一致，并且把恢复时间
    整体推迟一个 lease 周期（演练里因此出现"进程早被杀掉、run 却仍不算 stale"）。
    """
    _ = settings  # 保留参数以便调用方统一传入；判定不再依赖 lease 长度
    now = datetime.now(UTC)
    recovered: list[UUID] = []

    async with db.transaction() as session:
        stmt = select(RunRow).where(
            RunRow.status == str(RunStatus.RUNNING),
            (RunRow.lease_until.is_(None)) | (RunRow.lease_until < now),
        )
        if only_run_id is not None:
            stmt = stmt.where(RunRow.id == only_run_id)
        if owner_id is not None:
            from agent_workbench.runtime.selection import owned_run_ids

            stmt = stmt.where(RunRow.id.in_(owned_run_ids(owner_id)))
        rows = (await session.execute(stmt)).scalars().all()
        for run in rows:
            run.status = str(RunStatus.RECOVERING)
            run.owner = None
            run.lease_until = None
            await append_event(
                session,
                session_id=run.session_id,
                run_id=run.id,
                event_type=EventType.RUN_STATUS,
                payload={
                    "status": str(RunStatus.RECOVERING),
                    "reason": "lease_expired_on_startup",
                    "note": (
                        "先核对远端任务与外部副作用，再决定继续/暂停/等待用户；"
                        "checkpoint 不包含远端进程状态。"
                    ),
                },
            )
            recovered.append(run.id)
    return recovered


async def reconcile_remote_jobs(
    db: Database,
    settings: Settings | None = None,
    *,
    only_run_id: UUID | None = None,
    owner_id: str | None = None,
) -> ReconciliationReport:
    """核对控制端 `tool_jobs` 投影与 Executor 台账。

    - 能确认终态 -> 更新投影；
    - 能确认仍在运行 -> 保留 running 并标注"已核对"；
    - 无法核实 -> 标 `unknown`，**不自动重做**。
    """
    cfg = settings or get_settings()
    report = ReconciliationReport()

    async with db.session() as session:
        jobs = ToolJobRepository(session)
        tracked = list(
            await jobs.list_for_run(only_run_id, active_only=True)
            if only_run_id is not None
            else await jobs.list_active(cfg.executor_id)
        )
        if owner_id is not None:
            from agent_workbench.persistence.tables import ToolOperationRow
            from agent_workbench.runtime.selection import owned_run_ids

            allowed_operations = set(
                (
                    await session.execute(
                        select(ToolOperationRow.operation_id).where(
                            ToolOperationRow.run_id.in_(owned_run_ids(owner_id))
                        )
                    )
                )
                .scalars()
                .all()
            )
            tracked = [job for job in tracked if job.operation_id in allowed_operations]
        session_by_operation: dict[UUID, UUID] = {}
        if tracked:
            from agent_workbench.persistence.tables import ToolOperationRow

            rows = (
                await session.execute(
                    select(ToolOperationRow.operation_id, ToolOperationRow.session_id).where(
                        ToolOperationRow.operation_id.in_([item.operation_id for item in tracked])
                    )
                )
            ).all()
            session_by_operation = dict(rows)
    if not tracked:
        report.notes.append("没有需要核对的进行中任务")

    tunnel = SshTunnel(cfg)
    client = HttpExecutorClient(tunnel, settings=cfg, expected_executor_id=cfg.executor_id)
    try:
        await client.start()
        await client.capabilities()
        report.executor_reachable = True
    except (TunnelUnavailable, ExecutorConnectionError) as exc:
        report.executor_reachable = False
        report.notes.append(
            f"执行岛不可达，无法核对 {len(tracked)} 个任务：{type(exc).__name__}: {exc}。"
            "这些任务标为 unknown，不自动重跑。"
        )
        for row in tracked:
            await _mark_unknown(
                db,
                row.job_id,
                row.operation_id,
                str(row.executor_id),
                reason="执行岛不可达，本轮无法核对；未自动重跑。",
                session_id=session_by_operation.get(row.operation_id),
            )
            report.jobs_unverifiable.append(str(row.job_id))
        await _close(tunnel, client)
        return report

    try:
        # 有界并发核对：每个 job 都要经 SSH 隧道往返一次；串行在 job 数量上百时
        # 会把恢复流程拖到几分钟（演练里真实发生过，导致 Runner 迟迟不进入正常调度）。
        semaphore = asyncio.Semaphore(JOB_RECONCILE_CONCURRENCY)

        async def _probe(row: Any) -> tuple[Any, Any, str | None]:
            async with semaphore:
                try:
                    return row, await client.get_job(row.job_id), None
                except ExecutorRejected as exc:
                    return row, None, f"rejected:{exc.status_code}"
                except (ExecutorConnectionError, ExecutorProtocolError) as exc:
                    return row, None, f"unreachable:{exc}"
                except Exception as exc:
                    return row, None, f"unexpected:{type(exc).__name__}: {exc}"

        probed = await asyncio.gather(*(_probe(row) for row in tracked))

        for row, snapshot, problem in probed:
            if problem is not None:
                kind, _, detail = problem.partition(":")
                if kind == "rejected":
                    # 执行岛明确说"没有这个 job"。这**不等于**"没有发生副作用"：
                    # 可能是执行岛重启/台账被清理，也可能是任务从未被接收。
                    # 无法区分 -> 记 unknown，并要求用户确认（不自动重跑）。
                    note = (
                        f"job {row.job_id} 在执行岛不存在（HTTP {detail}）："
                        "无法区分『从未接收』与『执行岛已无记录』，标为 unknown。"
                    )
                elif kind == "unreachable":
                    note = f"job {row.job_id} 无法查询：{detail}"
                else:
                    note = f"job {row.job_id} 核对时出现意外错误：{detail}"
                await _mark_unknown(
                    db,
                    row.job_id,
                    row.operation_id,
                    str(row.executor_id),
                    reason=note,
                    session_id=session_by_operation.get(row.operation_id),
                )
                report.jobs_unverifiable.append(str(row.job_id))
                report.notes.append(note)
                continue

            async with db.transaction() as session:
                await ToolJobRepository(session).upsert(
                    job_id=row.job_id,
                    operation_id=row.operation_id,
                    executor_id=row.executor_id,
                    state=str(snapshot.state),
                    output_cursor=snapshot.output_cursor,
                    sync_status="in_sync",
                    executor_started_at=snapshot.started_at,
                    executor_finished_at=snapshot.finished_at,
                )
            report.jobs_reconciled.append({"job_id": str(row.job_id), "state": str(snapshot.state)})
            if str(snapshot.state) == "unknown":
                report.jobs_unverifiable.append(str(row.job_id))
    finally:
        await _close(tunnel, client)

    if only_run_id is None:
        report.orphan_artifacts_flagged = await flag_orphan_artifacts(db)
    return report


async def _mark_unknown(
    db: Database,
    job_id: UUID,
    operation_id: UUID,
    executor_id: str,
    *,
    reason: str | None = None,
    session_id: UUID | None = None,
) -> None:
    async with db.transaction() as session:
        await ToolJobRepository(session).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id=executor_id,
            state="unknown",
            sync_status="uncertain",
        )
        if session_id is not None:
            await append_event(
                session,
                session_id=session_id,
                event_type=EventType.ERROR,
                payload={
                    "scope": "job_state_unknown",
                    "job_id": str(job_id),
                    "message": reason or "任务状态无法核实；未自动重跑。",
                },
            )


async def _close(tunnel: SshTunnel, client: HttpExecutorClient) -> None:
    with contextlib.suppress(Exception):
        await client.close()
    with contextlib.suppress(Exception):
        await tunnel.close()


async def flag_orphan_artifacts(db: Database) -> list[str]:
    """找出元数据标 available 但文件缺失的工件，标记为 orphaned。

    **不得**把缺失文件的工件当作可用证据（§5.3）。
    """
    from agent_workbench.evidence.artifacts import build_artifact_store

    store = build_artifact_store()
    flagged: list[str] = []
    async with db.transaction() as session:
        rows = (
            (
                await session.execute(
                    select(ArtifactRow).where(ArtifactRow.availability == "available")
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if store.exists(row.relative_key):
                continue
            row.availability = "orphaned"
            flagged.append(str(row.id))
    if flagged:
        log.warning("orphan_artifacts_flagged", count=len(flagged), ids=flagged[:20])
    return flagged


async def run_startup_recovery(
    db: Database | None = None,
    settings: Settings | None = None,
    *,
    only_run_id: UUID | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Runner 启动时的核对入口。

    **顺序不能颠倒**（这是被验收指出的一处真实设计缺陷）：

    1. 把 lease 过期的 `running` run 标成 `recovering`（**只标记，不做排队决定**）；
    2. **核对远端 job**：这是判断"能不能继续"的前提。框架 checkpoint 不包含远端
       进程状态；不核对就继续，等于在无证据的情况下假设副作用没发生；
    3. **按核对结果决定归宿**：
       - 相关 job 全部能确认终态 -> 可以 `queued` 继续；
       - 存在 `unknown` / `sync_status=uncertain` / 执行岛不可达 -> **保持 `recovering`**，
         写清原因与处置入口，**绝不自动续跑**。

    早期实现是"先排队、后核对"，顺序做反了：远端状态未知的 run 也会被重新排队。
    """
    database = db or get_database()
    cfg = settings or get_settings()
    report = ReconciliationReport()

    # 1) 只标记，不决定归宿
    report.runs_requeued = [
        str(item)
        for item in await recover_stale_runs(
            database, cfg, only_run_id=only_run_id, owner_id=owner_id
        )
    ]
    report.notes.append("已把 lease 过期的 running run 标为 recovering；归宿在远端核对之后决定。")

    # 2) 先核对远端真实状态
    reconciled = await reconcile_remote_jobs(
        database, cfg, only_run_id=only_run_id, owner_id=owner_id
    )
    report.jobs_reconciled = reconciled.jobs_reconciled
    report.jobs_unverifiable = reconciled.jobs_unverifiable
    report.orphan_artifacts_flagged = reconciled.orphan_artifacts_flagged
    report.executor_reachable = reconciled.executor_reachable
    report.notes.extend(reconciled.notes)

    # 3) 再决定每个 recovering run 能不能继续
    decision = await decide_recovering_runs(
        database,
        executor_reachable=reconciled.executor_reachable,
        only_run_id=only_run_id,
        owner_id=owner_id,
    )
    report.allowed_to_resume = decision["requeued"]
    report.blocked_pending_user = decision["blocked"]
    report.notes.extend(decision["notes"])
    return report.to_dict()


#: 核对远端 job 的并发上限：这是**资源初值**，不是任务总时长限制
JOB_RECONCILE_CONCURRENCY = 8

#: 这些 job 状态意味着"无法确认终态"，此时不允许自动续跑
UNVERIFIABLE_JOB_STATES = frozenset({"unknown"})
UNVERIFIABLE_SYNC_STATUSES = frozenset({"uncertain"})


async def decide_recovering_runs(
    db: Database,
    *,
    executor_reachable: bool,
    only_run_id: UUID | None = None,
    owner_id: str | None = None,
) -> dict[str, list[str]]:
    """按远端核对结果决定每个 `recovering` run 的归宿。

    判定规则：
    - 该 run 名下存在 `unknown` job 或 `sync_status=uncertain` 的任务 -> **阻塞**；
    - 执行岛不可达（本轮根本没核对成功）-> **阻塞**（"没查到"不等于"没有副作用"）；
    - 其余 -> 允许 `queued` 继续。

    阻塞的 run 写 `stop_reason` + `input.required` 事件；用户必须显式确认才能 resume。
    """
    from agent_workbench.persistence.tables import ToolJobRow, ToolOperationRow

    requeued: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []

    async with db.session() as session:
        stmt = select(RunRow).where(RunRow.status == str(RunStatus.RECOVERING))
        if only_run_id is not None:
            stmt = stmt.where(RunRow.id == only_run_id)
        if owner_id is not None:
            from agent_workbench.runtime.selection import owned_run_ids

            stmt = stmt.where(RunRow.id.in_(owned_run_ids(owner_id)))
        runs = (await session.execute(stmt)).scalars().all()
        if not runs:
            return {
                "requeued": [],
                "blocked": [],
                "notes": ["没有待决定的 recovering run"],
            }

        rows = (
            await session.execute(
                select(
                    ToolOperationRow.run_id,
                    ToolJobRow.job_id,
                    ToolJobRow.state,
                    ToolJobRow.sync_status,
                )
                .join(ToolJobRow, ToolJobRow.job_id == ToolOperationRow.job_id)
                .where(ToolOperationRow.run_id.in_([item.id for item in runs]))
            )
        ).all()

    unresolved: dict[UUID, list[str]] = {}
    for run_id, job_id, state, sync_status in rows:
        if state in UNVERIFIABLE_JOB_STATES or sync_status in UNVERIFIABLE_SYNC_STATUSES:
            unresolved.setdefault(run_id, []).append(f"{job_id}:{state}/{sync_status}")

    for run_row in runs:
        reasons: list[str] = []
        if not executor_reachable:
            reasons.append("执行岛不可达，本轮无法核对远端任务的真实状态")
        if unresolved.get(run_row.id):
            reasons.append(f"存在无法确认终态的任务：{unresolved[run_row.id][:5]}")

        async with db.transaction() as session:
            run = (
                await session.execute(
                    select(RunRow).where(RunRow.id == run_row.id).with_for_update()
                )
            ).scalar_one()

            if reasons:
                detail = "；".join(reasons)
                run.stop_reason = (
                    f"启动核对未能确认远端副作用，保持 recovering 且不自动续跑。"
                    f"原因：{detail}。"
                    "处置：确认这些任务的实际影响后，用 resume 显式确认未知任务再继续。"
                )
                await append_event(
                    session,
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type=EventType.INPUT_REQUIRED,
                    payload={
                        "status": str(RunStatus.RECOVERING),
                        "reason": "remote_state_unverifiable",
                        "detail": detail,
                        "unresolved_jobs": unresolved.get(run.id, []),
                        "executor_reachable": executor_reachable,
                        "requires_user_action": "resume_with_acknowledgement",
                        "message": (
                            "无法确认远端副作用是否已经发生，因此**没有**自动重跑。"
                            "请先确认这些任务的实际影响，再显式恢复。"
                        ),
                    },
                )
                blocked.append(str(run.id))
                continue

            # 走统一的状态迁移入口，而不是直接改字段：
            # 这样 `ALLOWED_TRANSITIONS` 才是唯一真源，非法迁移会被立刻发现
            # （recovering -> queued 曾经漏在迁移表里，就是靠这条路径暴露的）。
            await set_run_status(session, run_id=run.id, target=RunStatus.QUEUED)
            run.stop_reason = None
            await append_event(
                session,
                session_id=run.session_id,
                run_id=run.id,
                event_type=EventType.RUN_STATUS,
                payload={
                    "status": str(RunStatus.QUEUED),
                    "reason": "reconciled_before_requeue",
                    "message": "远端任务已全部核对到终态，允许继续。",
                },
            )
            requeued.append(str(run.id))

    if blocked:
        notes.append(
            f"{len(blocked)} 个 run 因远端状态无法核实而保持 recovering，"
            "需要用户显式确认后才能恢复。"
        )
    if requeued:
        notes.append(f"{len(requeued)} 个 run 在核对通过后重新排队。")
    return {"requeued": requeued, "blocked": blocked, "notes": notes}
