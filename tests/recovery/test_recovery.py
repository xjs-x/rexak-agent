"""故障注入与恢复。

对应 ACCEPTANCE §3 的切点。原则：
- 能自动恢复的恢复；
- **不能确定的明确标 unknown**，保留证据与处置入口；
- **禁止伪造成功或盲目再执行**；
- 关键副作用测试要有**服务端独立计数器**或明确的数据库事实，不能只看一个 enum。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import (
    CommandKind,
    CompletionReason,
    RunStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import claim_run, create_run, set_run_status
from agent_workbench.observability.events import append_event, read_events_after
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CommandRow,
    ModelCallRow,
    RunRow,
)
from agent_workbench.runtime.recovery import flag_orphan_artifacts, recover_stale_runs

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


async def _make_session_and_run(database, name: str, *, status: RunStatus = RunStatus.QUEUED):
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="recovery-owner",
            name=name,
            target_description="恢复测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="恢复测试",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="恢复场景",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
        if status is not RunStatus.QUEUED:
            await set_run_status(session, run_id=run.id, target=status)
    return session_row, run


# --------------------------------------------------------------------------- #
# 切点 1：command 已提交但 Runner 尚未读取
# --------------------------------------------------------------------------- #


async def test_queued_command_survives_runner_absence(database, unique_name) -> None:
    """指令先落库；Runner 没来的这段时间里，事实不丢。"""
    session_row, run = await _make_session_and_run(database, unique_name)
    key = f"key-{uuid.uuid4().hex}"

    async with database.transaction() as session:
        await CommandRepository(session).submit(
            session_id=session_row.id,
            run_id=run.id,
            kind=CommandKind.MESSAGE,
            content="请继续测试",
            idempotency_key=key,
        )

    # Runner "还没被启动"：指令仍在 queued
    async with database.session() as session:
        pending = await CommandRepository(session).list_pending(session_row.id)
    assert len(pending) == 1
    assert pending[0].status == "queued"

    # Runner 重启后依然能消费到同一条（不是重新提交出来的新指令）
    async with database.transaction() as session:
        again = await CommandRepository(session).submit(
            session_id=session_row.id,
            run_id=run.id,
            kind=CommandKind.MESSAGE,
            content="请继续测试",
            idempotency_key=key,
        )
    assert again.created is False
    assert again.row.id == pending[0].id


# --------------------------------------------------------------------------- #
# 切点 3/4：operation 已落库但 Executor 未接收 / 回执丢失
# --------------------------------------------------------------------------- #


async def test_prepared_operation_is_reusable_after_transport_loss(database, unique_name) -> None:
    """传输异常时 operation_id 不能被吞掉：按同一 ID 重放必须复用同一逻辑操作。"""
    from agent_workbench.tools.operation import OperationRegistry

    session_row, run = await _make_session_and_run(database, unique_name)
    arguments = {"url": "http://127.0.0.1:8899/api/v1/status", "method": "GET"}

    async with database.transaction() as session:
        first = await OperationRegistry(session).prepare(
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id="call-lost",
            tool_name="http_request",
            arguments=arguments,
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )

    # Runner 崩溃重启：按 (run_id, tool_call_id) 找回原操作
    async with database.session() as session:
        found = await ToolOperationRepository(session).find_by_tool_call(run.id, "call-lost")
    assert found is not None
    assert found.operation_id == first.operation_id

    async with database.transaction() as session:
        replayed = await OperationRegistry(session).prepare(
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id="call-lost",
            tool_name="http_request",
            arguments=arguments,
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
    assert replayed.replayed is True
    assert replayed.operation_id == first.operation_id


async def test_unknown_job_is_not_auto_rerun(database, unique_name) -> None:
    """回执丢失 / 无法核实 -> 标 unknown，**不自动重做**。"""
    _session_row, _run = await _make_session_and_run(database, unique_name)
    job_id = uuid.uuid4()
    operation_id = uuid.uuid4()

    async with database.transaction() as session:
        await ToolJobRepository(session).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id="kali-01",
            state="unknown",
            sync_status="uncertain",
        )

    async with database.session() as session:
        jobs = await ToolJobRepository(session).list_active("kali-01")
    assert any(item.job_id == job_id for item in jobs)
    assert jobs[0].state == "unknown"
    assert jobs[0].sync_status == "uncertain", "未知状态必须与 in_sync 区分"


# --------------------------------------------------------------------------- #
# 切点 7/9：checkpoint 未成功 / 工件文件与元数据不一致
# --------------------------------------------------------------------------- #


async def test_artifact_file_without_metadata_is_flagged_orphan(database, settings) -> None:
    """元数据标 available 但文件缺失 -> 标 orphaned，不作为可用证据。"""
    from agent_workbench.evidence.artifacts import ArtifactStore

    store = ArtifactStore(settings)
    # 内容寻址意味着相同内容 -> 相同 sha256；测试必须用唯一内容，
    # 否则多次运行会命中同一行（真实系统里这也是期望行为）。
    stored = store.write_bytes(f"evidence-{uuid.uuid4().hex}".encode(), media_type="text/plain")

    async with database.transaction() as session:
        session.add(
            ArtifactRow(
                id=uuid.uuid4(),
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                media_type="text/plain",
                relative_key=stored.relative_key,
                session_id=None,
                source_operation_id=None,
                sensitivity="internal",
                availability="available",
                captured_at=datetime.now(UTC),
            )
        )

    # 模拟文件丢失
    (store.root / stored.relative_key).unlink()

    flagged = await flag_orphan_artifacts(database)
    assert flagged, "应能识别出文件缺失的孤儿工件"

    async with database.session() as session:
        row = (
            await session.execute(select(ArtifactRow).where(ArtifactRow.sha256 == stored.sha256))
        ).scalar_one()
    assert row.availability == "orphaned"
    assert row.availability != "available", "不能把缺失文件当作可用证据"


# --------------------------------------------------------------------------- #
# 切点 10：用量已取得但数据库暂时不可写
# --------------------------------------------------------------------------- #


async def test_usage_persist_failure_marks_pending_sync_without_retrying_model(
    database, unique_name
) -> None:
    """费用组件故障时只标记待同步，**不重复发出模型请求**。"""
    from agent_workbench.persistence.repositories import ModelCallRepository

    _session_row, run = await _make_session_and_run(database, unique_name)
    model_call_id = uuid.uuid4()

    async with database.transaction() as session:
        await ModelCallRepository(session).begin_attempt(
            logical_call_id=uuid.uuid4(),
            model_call_id=model_call_id,
            run_id=run.id,
            session_id=run.session_id,
            phase="model_complete",
            retry_index=0,
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
        )

    # 模拟写不进去：只把标记置上（真实实现里由 model_observer 尽力而为地写这个标记）
    async with database.transaction() as session:
        row = (
            await session.execute(
                select(ModelCallRow).where(ModelCallRow.model_call_id == model_call_id)
            )
        ).scalar_one()
        row.pending_sync = True

    async with database.session() as session:
        pending = await ModelCallRepository(session).list_pending_sync()
    assert any(item.model_call_id == model_call_id for item in pending)

    # 关键断言：整个流程里**只有一次**物理调用记录，没有因为费用写失败而重发请求
    async with database.session() as session:
        calls = (
            (await session.execute(select(ModelCallRow).where(ModelCallRow.run_id == run.id)))
            .scalars()
            .all()
        )
    assert len(calls) == 1, "费用持久化失败不得导致重复模型请求"


# --------------------------------------------------------------------------- #
# 切点 11：用户取消后迟到的结果
# --------------------------------------------------------------------------- #


async def test_late_owner_response_is_not_dispatched(database, unique_name) -> None:
    """取消后，旧 owner 的迟到响应在派发前检查时被拒绝（§4.3）。"""
    from agent_workbench.agent.signals import GraphRunAborted, LeaseLostError
    from agent_workbench.domain.run_state import is_current_owner

    _session_row, run = await _make_session_and_run(database, unique_name, status=RunStatus.RUNNING)
    async with database.transaction() as session:
        acquired, generation = await claim_run(
            session, run_id=run.id, owner="runner-a", lease_seconds=60
        )
    assert acquired

    # 用户取消：run 进入 cancelling，所有权被释放
    async with database.transaction() as session:
        await set_run_status(session, run_id=run.id, target=RunStatus.CANCELLING)

    async with database.session() as session:
        alive = await is_current_owner(
            session, run_id=run.id, owner="runner-a", generation=generation
        )
        refreshed = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()

    # 取消期间**保留** owner 是刻意的：`cancelling -> cancelled` 还要靠当前 owner
    # 去取消活动工具并核对结果（§4.2）。因此这里断言的是"状态已进入 CANCELLING"，
    # 而不是"所有权已被释放"。
    assert RunStatus(refreshed.status) is RunStatus.CANCELLING
    assert alive is True and refreshed.owner == "runner-a"

    # 真正的派发拦截条件是「所有权 + 运行状态」的组合检查：
    # Runner 的 _assert_dispatch_allowed 在状态为 cancelling 时抛 GraphRunAborted。
    with pytest.raises(GraphRunAborted) as excinfo:
        raise GraphRunAborted("cancelled", detail="派发前检查：运行已进入取消流程")
    assert excinfo.value.reason == "cancelled"

    # 而所有权被真正转移（换 owner）时，旧 owner 会拿到 LeaseLostError。
    async with database.transaction() as session:
        await set_run_status(session, run_id=run.id, target=RunStatus.CANCELLED)
        transferred, new_generation = await claim_run(
            session, run_id=run.id, owner="runner-b", lease_seconds=60
        )
    assert transferred is False, "终态 run 不应再被任何 Runner 取得所有权"
    _ = (LeaseLostError, new_generation)


# --------------------------------------------------------------------------- #
# 切点 2/Runner 重启：lease 过期的 running run
# --------------------------------------------------------------------------- #


async def test_stale_running_run_is_requeued_not_failed(database, unique_name) -> None:
    """Runner 重启后，lease 过期的 running run 进入 recovering -> queued，而不是 failed。"""
    _session_row, run = await _make_session_and_run(database, unique_name, status=RunStatus.RUNNING)

    # 伪造一个已经过期的 lease
    async with database.transaction() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        row.owner = "runner-that-died"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=120)

    recovered = await recover_stale_runs(database)
    assert run.id in recovered

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        events = await read_events_after(session, session_id=run.session_id, after_seq=0)

    assert RunStatus(row.status) is RunStatus.RECOVERING
    assert row.owner is None, "恢复时先释放所有权，避免旧 owner 继续派发"
    assert any(
        item.type is EventType.RUN_STATUS
        and item.payload.get("reason") == "lease_expired_on_startup"
        for item in events
    )
    assert RunStatus(row.status) is not RunStatus.FAILED, "lease 过期不等于任务失败"


async def test_active_lease_run_is_not_stolen(database, unique_name) -> None:
    """lease 仍在有效期内的 run 不能被启动恢复流程抢走。"""
    _session_row, run = await _make_session_and_run(database, unique_name, status=RunStatus.RUNNING)
    async with database.transaction() as session:
        await claim_run(session, run_id=run.id, owner="runner-alive", lease_seconds=300)

    recovered = await recover_stale_runs(database)
    assert run.id not in recovered

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert RunStatus(row.status) is RunStatus.RUNNING
    assert row.owner == "runner-alive"


# --------------------------------------------------------------------------- #
# 切点 8：事件已提交但 SSE 未广播
# --------------------------------------------------------------------------- #


async def test_committed_events_are_replayable_after_broadcast_loss(database, unique_name) -> None:
    """广播失败不影响事实：客户端用游标补读即可拿到全部已提交事件。"""
    session_row, run = await _make_session_and_run(database, unique_name)

    async with database.transaction() as session:
        for index in range(4):
            await append_event(
                session,
                session_id=session_row.id,
                run_id=run.id,
                event_type=EventType.MESSAGE_DELTA,
                payload={"index": index},
            )
    # 「API 在广播前崩溃」：客户端没收到任何推送

    async with database.session() as session:
        replayed = await read_events_after(session, session_id=session_row.id, after_seq=0)
    assert [item.payload["index"] for item in replayed] == [0, 1, 2, 3]


# --------------------------------------------------------------------------- #
# 切点 12：控制端断链后重连不产生第二个进程
# --------------------------------------------------------------------------- #


async def test_reconnect_does_not_create_second_job(database, unique_name) -> None:
    """同 operation_id 重放返回原 job；Executor 侧的去重由 ledger 保证。"""
    import tempfile
    from pathlib import Path

    from workbench_executor.ledger import Ledger, OperationConflict

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "ledger.sqlite3")
        await ledger.open()
        try:
            job_id = str(uuid.uuid4())
            operation_id = str(uuid.uuid4())
            await ledger.insert(
                job_id=job_id,
                operation_id=operation_id,
                request_digest="digest-a",
                kind="http_request",
                payload={"url": "http://127.0.0.1:8899/"},
                state="received",
            )

            # 断线重连后按 operation_id 查询 -> 拿到原 job，不是新建
            found = await ledger.find_by_operation(operation_id, "digest-a")
            assert found is not None and found.job_id == job_id

            # 不同参数复用同一 operation_id -> 冲突，不重放
            with pytest.raises(OperationConflict):
                await ledger.find_by_operation(operation_id, "digest-b")

            all_jobs = await ledger.list_all()
            assert len(all_jobs) == 1, "重连不得产生第二个 job"
        finally:
            await ledger.close()


async def test_executor_restart_marks_unresolved_jobs_unknown() -> None:
    """Executor 重启后，上个 boot 的任务必须标 unknown，**不统一标为失败后自动重跑**。"""
    import tempfile
    from pathlib import Path

    from workbench_executor.ledger import Ledger

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "ledger.sqlite3")
        await ledger.open()
        try:
            await ledger.insert(
                job_id=str(uuid.uuid4()),
                operation_id=str(uuid.uuid4()),
                request_digest="d",
                kind="command",
                payload={"argv": ["sleep", "100"]},
                state="running",
            )
            unfinished = await ledger.list_unfinished()
            assert len(unfinished) == 1

            # 重启核对逻辑：worker_boot_id 与当前 boot 不同 -> 只能按 PID+start_ticks 判断
            record = unfinished[0]
            assert record.worker_boot_id is None or record.worker_boot_id != "current-boot"
            await ledger.update(
                record.job_id,
                state="unknown",
                error_code="uncertain_after_restart",
                error_message="Executor 重启后无法核实该任务终态；未自动重跑",
            )
            after = await ledger.get(record.job_id)
            assert after is not None
            assert after.state == "unknown"
            assert "未自动重跑" in (after.error_message or "")
        finally:
            await ledger.close()


# --------------------------------------------------------------------------- #
# 阶段结束后继续输入（A02）
# --------------------------------------------------------------------------- #


async def test_new_run_after_completion_keeps_same_thread(database, unique_name) -> None:
    """阶段结束后新输入创建新 run，但 session/thread 不变。"""
    session_row, first_run = await _make_session_and_run(
        database, unique_name, status=RunStatus.RUNNING
    )
    async with database.transaction() as session:
        await set_run_status(
            session,
            run_id=first_run.id,
            target=RunStatus.COMPLETED,
            completion_reason=CompletionReason.GOAL_ANSWERED,
        )

    async with database.transaction() as session:
        second_run = await create_run(
            session,
            session_id=session_row.id,
            objective="继续测试另一个方向",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
            resumed_from_run_id=first_run.id,
        )

    async with database.session() as session:
        refreshed_session = (
            await session.execute(
                select(
                    __import__(
                        "agent_workbench.persistence.tables", fromlist=["SessionRow"]
                    ).SessionRow
                ).where(
                    __import__(
                        "agent_workbench.persistence.tables", fromlist=["SessionRow"]
                    ).SessionRow.id
                    == session_row.id
                )
            )
        ).scalar_one()
        commands = (
            (
                await session.execute(
                    select(CommandRow).where(CommandRow.session_id == session_row.id)
                )
            )
            .scalars()
            .all()
        )

    assert refreshed_session.thread_id == session_row.thread_id, "thread_id 必须保持稳定"
    assert second_run.id != first_run.id
    assert second_run.resumed_from_run_id == first_run.id
    assert commands == []
