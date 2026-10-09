"""核对远端任务时的异常分类。

针对演练中真实抓到的缺陷：控制端 `tool_jobs` 里有一条记录，但执行岛返回 404
（`job 不存在`）。早期实现只捕获 `ExecutorConnectionError | ExecutorProtocolError`，
`ExecutorRejected`（4xx）漏网，导致**整个启动核对抛异常、Runner 直接退出**。

正确语义：
- 404 **不等于**"没有发生副作用" —— 可能是执行岛重启/台账被清理，也可能是从未接收；
- 无法区分时只能记 `unknown` 并要求用户确认，**不自动重跑**；
- 单个 job 的意外不得中断整次核对。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import create_run, set_run_status
from agent_workbench.execution.client import ExecutorRejected
from agent_workbench.persistence.repositories import (
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import RunRow, ToolJobRow
from agent_workbench.runtime import recovery as recovery_module
from agent_workbench.runtime.recovery import (
    decide_recovering_runs,
    reconcile_remote_jobs,
    recover_stale_runs,
)

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


class _StubClient:
    """执行岛替身：get_job 按给定行为反应。"""

    def __init__(self, behaviour: str) -> None:
        self._behaviour = behaviour
        self.closed = False

    async def start(self) -> None:
        return None

    async def capabilities(self) -> Any:
        from agent_workbench.contracts.tool import ExecutorCapabilities

        return ExecutorCapabilities(
            executor_id="kali-01",
            service_version="0.1.0",
            protocol_version=1,
            boot_id="stub",
            started_at=datetime.now(UTC),
        )

    async def get_job(self, job_id: UUID) -> Any:
        if self._behaviour == "404":
            raise ExecutorRejected(f"查询 job={job_id} 被拒绝（HTTP 404）", status_code=404)
        if self._behaviour == "boom":
            raise RuntimeError("unexpected stub failure")
        raise AssertionError(f"未预期的行为：{self._behaviour}")

    async def close(self) -> None:
        self.closed = True


class _StubTunnel:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _recovering_run_with_tracked_job(database, name: str):
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="404-owner",
            name=name,
            target_description="404 核对测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="404 核对",
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
            request_digest="d",
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
            state="running",
        )
    return session_row, run, job_id


def _patch_client(monkeypatch: pytest.MonkeyPatch, behaviour: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> _StubClient:
        client = _StubClient(behaviour)
        captured["client"] = client
        return client

    monkeypatch.setattr(recovery_module, "HttpExecutorClient", factory)
    monkeypatch.setattr(recovery_module, "SshTunnel", _StubTunnel)
    return captured


async def test_404_is_recorded_as_unknown_not_a_crash(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行岛 404 -> 记 unknown 并阻塞续跑；核对本身**不能**抛异常。"""
    _session_row, run, job_id = await _recovering_run_with_tracked_job(database, unique_name)
    await recover_stale_runs(database)
    _patch_client(monkeypatch, "404")

    report = await reconcile_remote_jobs(database)

    assert report.executor_reachable is True, "执行岛是可达的，只是没有这个 job"
    assert str(job_id) in report.jobs_unverifiable
    assert any("在执行岛不存在" in note for note in report.notes)

    async with database.session() as session:
        job = (
            await session.execute(select(ToolJobRow).where(ToolJobRow.job_id == job_id))
        ).scalar_one()
    assert job.state == "unknown"
    assert job.sync_status == "uncertain", "未知状态必须与 in_sync 区分"

    decision = await decide_recovering_runs(database, executor_reachable=True)
    assert str(run.id) in decision["blocked"], "404 无法证明副作用没发生，必须阻塞而不是续跑"


async def test_unexpected_error_per_job_does_not_abort_reconciliation(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个 job 出现意外错误时，其余 job 仍要被核对完。"""
    _session_row, _run, job_id = await _recovering_run_with_tracked_job(database, unique_name)
    await recover_stale_runs(database)
    _patch_client(monkeypatch, "boom")

    report = await reconcile_remote_jobs(database)

    assert str(job_id) in report.jobs_unverifiable
    assert any("意外错误" in note for note in report.notes)
    async with database.session() as session:
        job = (
            await session.execute(select(ToolJobRow).where(ToolJobRow.job_id == job_id))
        ).scalar_one()
    assert job.state == "unknown"
