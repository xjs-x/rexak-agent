"""启动恢复的**顺序**与闸门（BUILD_PLAN §6.4、ACCEPTANCE §3 切点 2）。

针对一处真实设计缺陷：早期的 `run_startup_recovery` 是
「先把 recovering 的 run 改成 queued，再去核对远端 job」——顺序做反了，
远端状态未知的 run 也会被重新排队，等于在无证据的情况下继续产生副作用。

契约：
1. 先标记 recovering（只标记）；
2. 再核对远端 job；
3. 按核对结果决定归宿：只有"全部能确认终态"才允许 queued；
   存在 unknown / sync_status=uncertain / 执行岛不可达 -> 保持 recovering，
   写原因 + 可处置事件，**不自动续跑**。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import assert_resumable, create_run, set_run_status
from agent_workbench.persistence.repositories import (
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import EventRow, RunRow
from agent_workbench.runtime.recovery import (
    decide_recovering_runs,
    reconcile_remote_jobs,
    recover_stale_runs,
    run_startup_recovery,
)

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


async def _recovering_run_with_job(
    database, name: str, *, job_state: str, sync_status: str = "in_sync"
):
    """造一个"Runner 崩了、留下一个远端任务"的场景。"""
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="order-owner",
            name=name,
            target_description="恢复顺序测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="恢复顺序",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
        await set_run_status(session, run_id=run.id, target=RunStatus.RUNNING)

    # 伪造"上一次运行留下的过期 lease"
    async with database.transaction() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        row.owner = "runner-that-died"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=120)

    operation_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with database.transaction() as session:
        await ToolOperationRepository(session).prepare(
            operation_id=operation_id,
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id=f"call-{uuid.uuid4().hex[:6]}",
            tool_name="http_request",
            request_digest="digest",
            effect_class="read_only",
            policy_revision=1,
            scope_revision=1,
        )
        await ToolOperationRepository(session).attach_job(operation_id, job_id, status="running")
    async with database.transaction() as session:
        await ToolJobRepository(session).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id="kali-01",
            state=job_state,
            sync_status=sync_status,
        )

    return session_row, run, job_id


async def test_recover_stale_runs_only_marks_not_requeues(database, unique_name) -> None:
    """第一步只能**标记**，不能在核对之前把 run 变成可跑状态。"""
    _session_row, run, _job_id = await _recovering_run_with_job(
        database, unique_name, job_state="running"
    )

    recovered = await recover_stale_runs(database)
    assert run.id in recovered

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.RECOVERING
    assert RunStatus(row.status) is not RunStatus.QUEUED, "核对之前不得进入可跑状态"


async def test_unknown_job_blocks_requeue_and_requires_user(database, unique_name) -> None:
    """远端状态未知 -> 保持 recovering，写原因与处置入口，不自动续跑。"""
    session_row, run, job_id = await _recovering_run_with_job(
        database, unique_name, job_state="unknown"
    )
    await recover_stale_runs(database)

    decision = await decide_recovering_runs(database, executor_reachable=True)
    assert str(run.id) in decision["blocked"]
    assert str(run.id) not in decision["requeued"]

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        events = (
            (await session.execute(select(EventRow).where(EventRow.session_id == session_row.id)))
            .scalars()
            .all()
        )

    assert RunStatus(row.status) is RunStatus.RECOVERING
    assert row.stop_reason and "不自动续跑" in row.stop_reason
    required = [item for item in events if str(item.type) == str(EventType.INPUT_REQUIRED)]
    assert required, "必须留下 input.required 事件说明需要用户介入"
    payload = required[-1].payload
    assert payload["reason"] == "remote_state_unverifiable"
    assert payload["requires_user_action"] == "resume_with_acknowledgement"
    assert str(job_id) in str(payload["unresolved_jobs"])


async def test_uncertain_sync_status_blocks_requeue(database, unique_name) -> None:
    """`sync_status=uncertain` 与 unknown 同义：都表示无法确认终态。"""
    _session_row, run, _job_id = await _recovering_run_with_job(
        database, unique_name, job_state="running", sync_status="uncertain"
    )
    await recover_stale_runs(database)

    decision = await decide_recovering_runs(database, executor_reachable=True)
    assert str(run.id) in decision["blocked"]

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.RECOVERING


async def test_clean_run_is_requeued_after_reconciliation(database, unique_name) -> None:
    """所有相关 job 都能确认终态 -> 允许继续。"""
    session_row, run, _job_id = await _recovering_run_with_job(
        database, unique_name, job_state="succeeded"
    )
    await recover_stale_runs(database)

    decision = await decide_recovering_runs(database, executor_reachable=True)
    assert str(run.id) in decision["requeued"]
    assert str(run.id) not in decision["blocked"]

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        events = (
            (await session.execute(select(EventRow).where(EventRow.session_id == session_row.id)))
            .scalars()
            .all()
        )
    assert RunStatus(row.status) is RunStatus.QUEUED
    assert row.stop_reason is None
    assert any(item.payload.get("reason") == "reconciled_before_requeue" for item in events), (
        "重新排队必须留下'已核对'的依据"
    )


async def test_executor_unreachable_blocks_requeue(database, unique_name) -> None:
    """执行岛不可达时，"没查到未结任务"不能当成"没有副作用"。"""
    _session_row, run, _job_id = await _recovering_run_with_job(
        database, unique_name, job_state="succeeded"
    )
    await recover_stale_runs(database)

    decision = await decide_recovering_runs(database, executor_reachable=False)
    assert str(run.id) in decision["blocked"], "核对本身失败时不得续跑"
    assert str(run.id) not in decision["requeued"]

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.RECOVERING
    assert "执行岛不可达" in (row.stop_reason or "")


async def test_startup_recovery_reconciles_before_requeue(database, unique_name, settings) -> None:
    """端到端顺序不变式：执行岛不可达时，**任何** run 都不得变成 queued。

    通过把 executor_host 指到 TEST-NET-1（192.0.2.0/24，不可路由）来强制核对失败。
    """
    _session_row, run, _job_id = await _recovering_run_with_job(
        database, unique_name, job_state="succeeded"
    )
    unreachable = settings.model_copy(update={"executor_host": "192.0.2.1"})

    report = await run_startup_recovery(database, unreachable)

    assert report["executor_reachable"] is False
    allowed = [str(item) for item in report["allowed_to_resume"]]  # type: ignore[union-attr]
    blocked = [str(item) for item in report["blocked_pending_user"]]  # type: ignore[union-attr]
    assert str(run.id) not in allowed
    assert str(run.id) in blocked

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.RECOVERING, (
        "核对失败时不得排队 —— 这正是早期实现做反的地方"
    )


async def test_reconcile_returns_unreachable_without_claiming_success(database, settings) -> None:
    """核对函数自身在不可达时必须如实返回，不能"看起来核对了"。"""
    unreachable = settings.model_copy(update={"executor_host": "192.0.2.1"})
    report = await reconcile_remote_jobs(database, unreachable)
    assert report.executor_reachable is False
    assert report.jobs_reconciled == []


async def test_recovering_is_resumable_domain_level(database, unique_name) -> None:
    """`recovering` 在领域层是可恢复状态（恢复动作本身由 API 强制要求显式确认）。"""
    _session_row, run, _job_id = await _recovering_run_with_job(
        database, unique_name, job_state="unknown"
    )
    await recover_stale_runs(database)

    async with database.session() as session:
        row = await assert_resumable(session, run.id)
    assert RunStatus(row.status) is RunStatus.RECOVERING
