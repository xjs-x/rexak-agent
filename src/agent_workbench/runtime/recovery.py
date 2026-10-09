"""恢复：checkpoint 与远端任务核对。

规则（§6.4、§8.4、ACCEPTANCE §3）：
- 框架 graph checkpoint **不包含远端进程**；恢复前**必须**核对 job 状态；
- 重启后原任务可能仍在运行、已退出或无法核实 —— **分别核对**，
  **不统一标为失败后自动重跑**；
- 恢复现有 interrupt 用其规定的 resume 方式；已完成阶段的新消息用普通输入继续同一 thread；
- 无法确定时明确标 `unknown` 并保留证据与处置入口，**禁止伪造成功或盲目再执行**。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
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
    jobs_reconciled: list[dict[str, str]] = field(default_factory=list)
    jobs_unverifiable: list[str] = field(default_factory=list)
    orphan_artifacts_flagged: list[str] = field(default_factory=list)
    executor_reachable: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "runs_requeued": self.runs_requeued,
            "jobs_reconciled": self.jobs_reconciled,
            "jobs_unverifiable": self.jobs_unverifiable,
            "orphan_artifacts_flagged": self.orphan_artifacts_flagged,
            "executor_reachable": self.executor_reachable,
            "notes": self.notes,
        }


async def recover_stale_runs(db: Database, settings: Settings | None = None) -> list[UUID]:
    """把 lease 已过期的 `running` run 置为 `recovering` 并**重新排队**。

    不直接跑：先让核对流程检查远端副作用，再决定继续还是等待用户。
    """
    cfg = settings or get_settings()
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=cfg.run_lease_seconds)
    recovered: list[UUID] = []

    async with db.transaction() as session:
        rows = (
            (
                await session.execute(
                    select(RunRow).where(
                        RunRow.status == str(RunStatus.RUNNING),
                        (RunRow.lease_until.is_(None)) | (RunRow.lease_until < stale_cutoff),
                    )
                )
            )
            .scalars()
            .all()
        )
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
    db: Database, settings: Settings | None = None
) -> ReconciliationReport:
    """核对控制端 `tool_jobs` 投影与 Executor 台账。

    - 能确认终态 -> 更新投影；
    - 能确认仍在运行 -> 保留 running 并标注"已核对"；
    - 无法核实 -> 标 `unknown`，**不自动重做**。
    """
    cfg = settings or get_settings()
    report = ReconciliationReport()

    async with db.session() as session:
        tracked = list(await ToolJobRepository(session).list_active(cfg.executor_id))
    if not tracked:
        report.notes.append("没有需要核对的进行中任务")
        return report

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
            await _mark_unknown(db, row.job_id, row.operation_id, str(row.executor_id))
            report.jobs_unverifiable.append(str(row.job_id))
        await _close(tunnel, client)
        return report

    try:
        for row in tracked:
            try:
                snapshot = await client.get_job(row.job_id)
            except (ExecutorConnectionError, ExecutorProtocolError) as exc:
                await _mark_unknown(db, row.job_id, row.operation_id, str(row.executor_id))
                report.jobs_unverifiable.append(str(row.job_id))
                report.notes.append(f"job {row.job_id} 无法查询：{exc}")
                continue

            async with db.transaction() as session:
                await ToolJobRepository(session).upsert(
                    job_id=row.job_id,
                    operation_id=row.operation_id,
                    executor_id=row.executor_id,
                    state=str(snapshot.state),
                    output_cursor=snapshot.output_cursor,
                    sync_status="in_sync",
                )
            report.jobs_reconciled.append({"job_id": str(row.job_id), "state": str(snapshot.state)})
            if str(snapshot.state) == "unknown":
                report.jobs_unverifiable.append(str(row.job_id))
    finally:
        await _close(tunnel, client)

    report.orphan_artifacts_flagged = await flag_orphan_artifacts(db)
    return report


async def _mark_unknown(db: Database, job_id: UUID, operation_id: UUID, executor_id: str) -> None:
    async with db.transaction() as session:
        await ToolJobRepository(session).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id=executor_id,
            state="unknown",
            sync_status="uncertain",
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
    db: Database | None = None, settings: Settings | None = None
) -> dict[str, object]:
    """Runner 启动时的核对入口。"""
    database = db or get_database()
    cfg = settings or get_settings()
    report = ReconciliationReport()

    report.runs_requeued = [str(item) for item in await recover_stale_runs(database, cfg)]

    if report.runs_requeued:
        async with database.transaction() as session:
            rows = (
                (
                    await session.execute(
                        select(RunRow).where(
                            RunRow.status == str(RunStatus.RECOVERING),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for run in rows:
                # 核对过远端副作用后才允许继续；这里保守地先转为 queued，
                # 由 Runner 在真正派发前再次检查控制版本与所有权。
                run.status = str(RunStatus.QUEUED)

    reconciled = await reconcile_remote_jobs(database, cfg)
    report.jobs_reconciled = reconciled.jobs_reconciled
    report.jobs_unverifiable = reconciled.jobs_unverifiable
    report.orphan_artifacts_flagged = reconciled.orphan_artifacts_flagged
    report.executor_reachable = reconciled.executor_reachable
    report.notes.extend(reconciled.notes)
    return report.to_dict()
