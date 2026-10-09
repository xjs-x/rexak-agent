"""后台任务唤醒（`waiting_tool`）。

针对 BUILD_PLAN §8.3 与 §17"等待工具期间模型空转"：

- 有非终态后台任务时 run 进入 `waiting_tool` 并**返回**，不继续调模型；
- 由唤醒器按间隔核对远端状态；全部**有确定结论**后才重新排队；
- **`unknown` 不算确定结论** —— 据它唤醒等于在未知副作用上继续推进；
- 唤醒通知作为一条 MESSAGE 指令入队（复用既有注入通路），
  **不伪造对应原 tool_call 的 tool 消息**。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import CommandKind, JobState, RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.contracts.tool import JobSnapshot
from agent_workbench.domain.run_state import claim_run, create_run, set_run_status
from agent_workbench.execution.tunnel import TunnelUnavailable
from agent_workbench.persistence.repositories import (
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import (
    CommandRow,
    EventRow,
    RunRow,
    ToolJobRow,
)
from agent_workbench.runtime import jobs as jobs_module
from agent_workbench.runtime.jobs import (
    WAKEUP_MARKER,
    pending_jobs_for_run,
    probe_and_wake,
)

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


class _StubClient:
    """执行岛替身：按 job_id -> 状态表返回，或统一抛错。"""

    def __init__(self, states: dict[str, str], *, fail: str | None = None) -> None:
        self._states = states
        self._fail = fail
        self.calls: list[str] = []

    async def start(self) -> None:
        if self._fail == "tunnel":
            raise TunnelUnavailable("stub: 执行岛不可达（连接阶段）")
        return

    async def get_job(self, job_id: UUID) -> Any:
        self.calls.append(str(job_id))
        if self._fail == "tunnel":
            raise jobs_module.TunnelUnavailable("stub: 执行岛不可达")
        state = self._states.get(str(job_id), "running")
        if state == "missing":
            raise jobs_module.ExecutorRejected("stub: job 不存在", status_code=404)

        return JobSnapshot(
            job_id=job_id,
            operation_id=uuid.uuid4(),
            kind="http_request",
            state=JobState(state),
            byte_count=10,
        )

    async def close(self) -> None:
        return None


class _StubTunnel:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        return None


def _patch(monkeypatch: pytest.MonkeyPatch, stub: _StubClient) -> None:
    monkeypatch.setattr(jobs_module, "HttpExecutorClient", lambda *_a, **_k: stub)
    monkeypatch.setattr(jobs_module, "SshTunnel", _StubTunnel)


async def _waiting_run_with_job(database, name: str):
    """造一个"已提交后台任务并进入 waiting_tool"的 run。"""
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="wakeup-owner",
            name=name,
            target_description="唤醒测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="唤醒",
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
        await claim_run(session, run_id=run.id, owner="wake-runner", lease_seconds=120)

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
            state=str(JobState.RECEIVED),
        )
    async with database.transaction() as session:
        await set_run_status(session, run_id=run.id, target=RunStatus.WAITING_TOOL)
    return session_row, run, job_id


async def test_pending_jobs_listed_for_waiting_run(database, unique_name) -> None:
    _session_row, run, job_id = await _waiting_run_with_job(database, unique_name)
    pending = await pending_jobs_for_run(database, run.id)
    assert [str(item.job_id) for item in pending] == [str(job_id)]


async def test_still_running_keeps_run_waiting(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_row, run, job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({str(job_id): str(JobState.RUNNING)}))

    result = await probe_and_wake(database, run_ids=[run.id])

    assert str(run.id) in result["still_waiting"]
    assert result["woken"] == []
    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        commands = (
            (
                await session.execute(
                    select(CommandRow).where(CommandRow.session_id == session_row.id)
                )
            )
            .scalars()
            .all()
        )
    assert RunStatus(row.status) is RunStatus.WAITING_TOOL
    assert commands == [], "任务未结束不得注入任何消息（否则就是空转）"


async def test_all_terminal_wakes_run_and_injects_notice(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_row, run, job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({str(job_id): str(JobState.SUCCEEDED)}))

    result = await probe_and_wake(database, run_ids=[run.id])

    assert str(run.id) in result["woken"]
    assert str(job_id) in result["terminal"]

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        projection = (
            await session.execute(select(ToolJobRow).where(ToolJobRow.job_id == job_id))
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
        events = (
            (await session.execute(select(EventRow).where(EventRow.session_id == session_row.id)))
            .scalars()
            .all()
        )

    assert RunStatus(row.status) is RunStatus.QUEUED, "全部有确定结论后才重新排队"
    assert projection.state == str(JobState.SUCCEEDED)
    assert projection.sync_status == "in_sync"

    assert len(commands) == 1, "唤醒时注入一条系统通知"
    command = commands[0]
    assert command.kind == str(CommandKind.MESSAGE)
    assert WAKEUP_MARKER in command.content
    assert str(job_id) in command.content
    assert command.payload.get("source") == "job_wakeup"

    reasons = [item.payload.get("reason") for item in events]
    assert "jobs_reached_terminal" in reasons


async def test_wakeup_is_idempotent(database, unique_name, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复巡检不能给模型塞第二份同样的通知。"""
    session_row, _run, job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({str(job_id): str(JobState.SUCCEEDED)}))

    await probe_and_wake(database, run_ids=[_run.id])
    # 第二次：run 已不在 waiting_tool，自然不会再唤醒
    second = await probe_and_wake(database, run_ids=[_run.id])

    async with database.session() as session:
        commands = (
            (
                await session.execute(
                    select(CommandRow).where(CommandRow.session_id == session_row.id)
                )
            )
            .scalars()
            .all()
        )
    assert second["woken"] == []
    assert len(commands) == 1


async def test_unknown_job_does_not_wake_run(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**unknown 不是确定结论**：不得据此唤醒 run。

    这是关键语义：unknown 表示"既不能证明完成，也不能证明没发生"。
    据它唤醒等于在未知副作用之上继续推进。
    """
    session_row, run, job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({str(job_id): "missing"}))

    result = await probe_and_wake(database, run_ids=[run.id])

    assert result["woken"] == [], "unknown 不得触发唤醒"
    assert str(run.id) in result["still_waiting"]
    assert any("无法核实" in note for note in result["notes"])

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        projection = (
            await session.execute(select(ToolJobRow).where(ToolJobRow.job_id == job_id))
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
    assert RunStatus(row.status) is RunStatus.WAITING_TOOL
    assert projection.state == str(JobState.UNKNOWN)
    assert projection.sync_status == "uncertain"
    assert commands == []


async def test_executor_unreachable_keeps_run_waiting_without_model_calls(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行岛不可达时保持等待：不唤醒、不注入消息、**不调用模型**。"""
    session_row, run, _job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({}, fail="tunnel"))

    result = await probe_and_wake(database, run_ids=[run.id])

    assert result["woken"] == []
    assert result["probed"] == 0
    assert any("执行岛不可达" in note for note in result["notes"])

    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        commands = (
            (
                await session.execute(
                    select(CommandRow).where(CommandRow.session_id == session_row.id)
                )
            )
            .scalars()
            .all()
        )
    assert RunStatus(row.status) is RunStatus.WAITING_TOOL
    assert commands == []


async def test_no_waiting_runs_is_cheap_noop(database, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有等待中的 run 时，巡检不得触碰执行岛。

    这里用**显式空集合**而不是依赖全局状态：测试库里可能残留其它用例留下的
    waiting_tool run，靠"全局没人等"来断言会被污染（第一次写就踩到了）。
    """
    stub = _StubClient({})
    _patch(monkeypatch, stub)
    result = await probe_and_wake(database, run_ids=[])
    assert result["probed"] == 0
    assert result["woken"] == []
    assert stub.calls == [], "没有等待中的 run 就不该访问执行岛"


async def test_failed_job_also_wakes(
    database, unique_name, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败也是确定结论：应唤醒 run 让模型知道失败并换路线。"""
    _session_row, run, job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({str(job_id): str(JobState.FAILED)}))

    result = await probe_and_wake(database, run_ids=[run.id])
    assert str(run.id) in result["woken"]


async def test_state_machine_allows_waiting_tool_to_queued() -> None:
    """状态机必须允许 waiting_tool -> queued（否则唤醒会被自己拒绝）。"""
    from agent_workbench.domain.run_state import can_transition

    assert can_transition(RunStatus.WAITING_TOOL, RunStatus.QUEUED)


async def test_waiting_run_without_commands_is_not_a_candidate(
    database, unique_name, settings
) -> None:
    """**"不空转"的机制性证明**：等待中的 run 不会被 Runner 取走去调模型。

    `_candidate_runs` 只把 waiting_tool 的 run 当作候选**当且仅当**它有待消费指令；
    而待消费指令只会在"所有任务都有确定结论"时由唤醒器注入。
    因此等待期间 Runner 根本不会驱动这个 run —— 不会有模型调用。
    """
    from agent_workbench.runtime.runner import AgentRunner

    _session_row, run, _job_id = await _waiting_run_with_job(database, unique_name)
    runner = AgentRunner(settings=settings, owner="wake-runner", database=database)

    candidates = await runner._candidate_runs(limit=50)
    assert run.id not in [item.id for item in candidates], (
        "等待中且没有待消费指令的 run 不能被取走（否则就是空转调模型）"
    )


async def test_woken_run_becomes_candidate_again(
    database, unique_name, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """唤醒后（有通知指令 + 已排队）Runner 应该能重新取到它。"""
    from agent_workbench.runtime.runner import AgentRunner

    _session_row, run, job_id = await _waiting_run_with_job(database, unique_name)
    _patch(monkeypatch, _StubClient({str(job_id): str(JobState.SUCCEEDED)}))
    result = await probe_and_wake(database, run_ids=[run.id])
    assert str(run.id) in result["woken"]

    runner = AgentRunner(settings=settings, owner="wake-runner", database=database)
    # 用 only_run_id 定向查询，避免被库里其它 queued run 的 limit 截断干扰
    candidates = await runner._candidate_runs(only_run_id=run.id)
    assert [item.id for item in candidates] == [run.id]


def test_wakeup_message_is_marked_as_system_notice() -> None:
    """注入文本必须能让模型区分"系统通知"与"用户消息"。"""
    assert WAKEUP_MARKER.startswith("【")
    text = jobs_module.wakeup_message("后台任务已结束：job-1:succeeded")
    assert WAKEUP_MARKER in text
    assert "不要重新提交同一个请求" in text


def test_terminal_classification() -> None:
    assert jobs_module.is_definite_terminal(str(JobState.SUCCEEDED))
    assert jobs_module.is_definite_terminal(str(JobState.FAILED))
    assert jobs_module.is_definite_terminal(str(JobState.CANCELLED))
    assert not jobs_module.is_definite_terminal(str(JobState.UNKNOWN))
    assert jobs_module.is_unresolved(str(JobState.UNKNOWN))
    assert not jobs_module.is_unresolved(str(JobState.SUCCEEDED))
