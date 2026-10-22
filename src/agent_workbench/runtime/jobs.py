"""后台任务唤醒（`waiting_tool`）。

问题（BUILD_PLAN §8.3、§17"等待工具期间模型空转"）：
模型拿到 `accepted` 之后如果继续跑，就会**反复调用模型问"任务好了吗"**；
如果直接阻塞等，又会把 Runner 卡住，连独立任务都推不动。

契约：

1. 片段结束时若有**非终态**后台任务 -> 把 run 置 `waiting_tool` 并**返回**，
   Runner 立刻去做别的事（同一 session 不可能有第二个 run，但 Runner 可以处理
   其它 session；更重要的是它不再空转调模型）；
2. 由一个**任务唤醒器**按可配间隔核对远端真实状态（有界并发，不密集轮询）；
3. 该 run 名下所有任务都到终态 -> 把 run 重新排队，并注入一条**明确的系统通知**，
   告诉模型发生了什么、下一步怎么做；
4. 完成通知**不伪造第二条对应原 tool_call 的 tool 消息** ——
   §8.3 明确要求"后续完成通知经专用事件入口合并"。

关于"事件唤醒"：Executor 提供 `GET /events?after=`，控制端可以使用增量事件。
首版用"低频轮询非终态任务"实现同一语义：**它由 Runner 自己的巡检驱动，
不消耗模型调用**，也不依赖进程内内存状态（每次从数据库重建）。
换成推送式唤醒只是把 `_probe_jobs` 的输入从轮询改成事件流，不影响上面的契约。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import CommandKind, JobState, RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.tool import execution_job_state
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
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ToolJobRepository,
)
from agent_workbench.persistence.tables import (
    ExecutionResourceRow,
    RunRow,
    ToolJobRow,
    ToolOperationRow,
)

log = get_logger("agent_workbench.jobs")

#: 尚未有**确定结论**的任务状态。
#:
#: 注意 `unknown` 也算未决：`unknown` 的含义是"既不能证明完成，也不能证明没发生"，
#: 它**不是**终态结论。如果因为 unknown 就唤醒 run，模型会在未知副作用之上继续推进 ——
#: 这正是 §6.5 第 6 条要避免的。unknown 只能由用户确认后（resume）处理。
UNRESOLVED_JOB_STATES = ("received", "starting", "running", "unknown")

#: 已有**确定结论**的终态（可以据此唤醒）
DEFINITE_TERMINAL_STATES = ("succeeded", "failed", "cancelled")

#: 核对远端状态的并发上限（资源初值，不是任务总时长限制）
PROBE_CONCURRENCY = 8

#: 注入给模型的唤醒通知标记，便于把"系统通知"与"用户消息"区分开
WAKEUP_MARKER = "【系统通知·后台任务】"


class JobWakeupResult(dict):
    """一次唤醒巡检的结果（作为 dict 使用，便于写进日志/事件）。"""


async def pending_jobs_for_run(db: Database, run_id: UUID) -> list[ToolJobRow]:
    """该 run 名下所有非终态任务。"""
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(ToolJobRow)
                    .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .outerjoin(
                        ExecutionResourceRow, ExecutionResourceRow.resource_id == ToolJobRow.job_id
                    )
                    .where(
                        (ExecutionResourceRow.resource_id.is_(None))
                        | (
                            ExecutionResourceRow.kind.in_(["command", "script"])
                            & (
                                ExecutionResourceRow.detail[
                                    "completion_role"
                                ].astext.is_distinct_from("resource")
                            )
                        )
                    )
                    .where(ToolOperationRow.run_id == run_id)
                    .where(ToolJobRow.state.in_(list(UNRESOLVED_JOB_STATES)))
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def open_wait_sets_for_run(db: Database, run_id: UUID) -> list[Any]:
    """该 run 名下**尚未满足**的明确等待集合。

    等待集合与后台任务同属"未完成工作"：不能仅改称"保留资源"绕过 finish_gate。
    """
    from agent_workbench.persistence.repositories import ExecutionWaitSetRepository

    async with db.session() as session:
        return list(await ExecutionWaitSetRepository(session).open_for_run(run_id))


async def run_has_pending_work(db: Database, run_id: UUID) -> tuple[list[ToolJobRow], list[Any]]:
    """(未决 job, 未满足 wait-set)。两者都为空才算"该 run 没有等待中的工作"。"""
    return (
        await pending_jobs_for_run(db, run_id),
        await open_wait_sets_for_run(db, run_id),
    )


async def runs_waiting_on_jobs(db: Database) -> list[UUID]:
    """所有 `waiting_tool` 的 run。"""
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(RunRow.id).where(RunRow.status == str(RunStatus.WAITING_TOOL))
                )
            )
            .scalars()
            .all()
        )
    return [UUID(str(item)) for item in rows]


async def probe_and_wake(
    db: Database,
    *,
    settings: Settings | None = None,
    run_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    """核对远端任务状态并唤醒可以继续的等待中 run。

    返回：`{"probed": n, "terminal": [...], "woken": [...], "still_waiting": [...], "notes": [...]}`。

    设计要点：
    - **只核对非终态任务**，已终态的不再打扰执行岛；
    - 单个任务的问题只影响该任务；
    - 唤醒条件是"该 run 名下**没有**非终态任务"，不是"某一个任务完成"；
    - 无法核实的任务保持 `unknown`，**不会**因此唤醒 run（避免在未知副作用上继续）。
    """
    cfg = settings or get_settings()
    result: dict[str, Any] = {
        "probed": 0,
        "terminal": [],
        "woken": [],
        "still_waiting": [],
        "notes": [],
    }

    waiting = run_ids if run_ids is not None else await runs_waiting_on_jobs(db)
    if not waiting:
        return result

    async with db.session() as session:
        tracked = list(
            (
                await session.execute(
                    select(ToolJobRow)
                    .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .outerjoin(
                        ExecutionResourceRow, ExecutionResourceRow.resource_id == ToolJobRow.job_id
                    )
                    .where(
                        (ExecutionResourceRow.resource_id.is_(None))
                        | (
                            ExecutionResourceRow.kind.in_(["command", "script"])
                            & (
                                ExecutionResourceRow.detail[
                                    "completion_role"
                                ].astext.is_distinct_from("resource")
                            )
                        )
                    )
                    .where(ToolOperationRow.run_id.in_(waiting))
                    .where(ToolJobRow.state.in_(list(UNRESOLVED_JOB_STATES)))
                )
            )
            .scalars()
            .all()
        )
        from agent_workbench.persistence.tables import ExecutionWaitSetRow

        wait_set_rows = (
            await session.execute(
                select(ExecutionWaitSetRow.run_id, ExecutionWaitSetRow.id).where(
                    ExecutionWaitSetRow.run_id.in_(waiting),
                    ExecutionWaitSetRow.status == "open",
                )
            )
        ).all()
    open_wait_sets: dict[UUID, list[str]] = {}
    for run_id, wait_set_id in wait_set_rows:
        if run_id is None:
            continue
        open_wait_sets.setdefault(run_id, []).append(str(wait_set_id))

    if not tracked:
        # 没有待核对任务：台账里已经没有未决记录（例如任务按 accepted 闭合后
        # 从没被真正接收，或上一次巡检已经同步过终态）。此时才可以直接唤醒；
        # 但仍在等待明确条件（wait-set 未满足）的 run 保持等待。
        async with db.session() as session:
            terminal_rows = (
                await session.execute(
                    select(ToolOperationRow.run_id, ToolJobRow.job_id)
                    .join(ToolJobRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .where(
                        ToolOperationRow.run_id.in_(waiting),
                        ToolJobRow.state.in_(list(DEFINITE_TERMINAL_STATES)),
                    )
                )
            ).all()
        terminal_ids: dict[UUID, list[UUID]] = {}
        for run_id, job_id in terminal_rows:
            terminal_ids.setdefault(run_id, []).append(job_id)
        for run_id in waiting:
            if open_wait_sets.get(run_id):
                result["still_waiting"].append(str(run_id))
                result["notes"].append(
                    f"run {run_id} 仍在等待明确条件（wait-set {open_wait_sets[run_id][:3]}）；"
                    "满足后由事件周期唤醒。"
                )
                continue
            if await _wake_run(
                db,
                run_id,
                summary=_no_pending_summary(),
                terminal_job_ids=terminal_ids.get(run_id, []),
            ):
                result["woken"].append(str(run_id))
        return result

    tunnel = SshTunnel(cfg)
    client = HttpExecutorClient(tunnel, settings=cfg, expected_executor_id=cfg.executor_id)
    try:
        await client.start()
    except (TunnelUnavailable, ExecutorConnectionError) as exc:
        result["notes"].append(
            f"执行岛不可达，本轮无法核对 {len(tracked)} 个后台任务："
            f"{type(exc).__name__}: {exc}。保持 waiting_tool，不空转调用模型。"
        )
        await close_quietly(client, tunnel)
        return result

    try:
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def _probe(row: ToolJobRow) -> tuple[ToolJobRow, str, str]:
            async with semaphore:
                try:
                    async with db.session() as session:
                        resource = await session.get(ExecutionResourceRow, row.job_id)
                    if resource is not None:
                        view = await client.execution_resource(str(row.job_id))
                        state = str(view.get("state") or "unknown")
                        if state in {"exited", "closed"}:
                            state = "succeeded" if view.get("exit_code") == 0 else "failed"
                        return row, state, ""
                    snapshot = await client.get_job(row.job_id)
                    return row, execution_job_state(snapshot), ""
                except ExecutorRejected as exc:
                    return row, "unknown", f"执行岛无该任务记录（HTTP {exc.status_code}）"
                except (ExecutorConnectionError, ExecutorProtocolError) as exc:
                    return row, "unknown", f"查询失败：{exc}"
                except Exception as exc:
                    return row, "unknown", f"意外错误：{type(exc).__name__}: {exc}"

        probed = await asyncio.gather(*(_probe(row) for row in tracked))
    finally:
        await close_quietly(client, tunnel)

    result["probed"] = len(probed)
    still_pending_by_run: dict[UUID, int] = {}
    terminal_by_run: dict[UUID, list[tuple[UUID, str]]] = {}
    unresolved_by_run: dict[UUID, list[str]] = {}

    async with db.session() as session:
        run_by_operation = dict(
            (
                await session.execute(
                    select(ToolOperationRow.operation_id, ToolOperationRow.run_id).where(
                        ToolOperationRow.operation_id.in_([row.operation_id for row in tracked])
                    )
                )
            ).all()
        )

    for row, state, problem in probed:
        run_id = run_by_operation.get(row.operation_id)
        if run_id is None:
            continue

        # 投影**始终**同步：`tool_jobs` 是执行岛状态的同步投影，
        # 观察到什么就记什么（含 unknown/uncertain），便于 UI 与诊断看到真相。
        async with db.transaction() as session:
            await ToolJobRepository(session).upsert(
                job_id=row.job_id,
                operation_id=row.operation_id,
                executor_id=row.executor_id,
                state=state,
                sync_status="in_sync" if state in DEFINITE_TERMINAL_STATES else "uncertain",
            )

            operation = await session.get(ToolOperationRow, row.operation_id)
            if operation is not None and operation.status in {"accepted", "running", "unknown"}:
                operation.status = state

        if state in UNRESOLVED_JOB_STATES:
            # 但**唤醒判定**必须另算：还没有确定结论就不能唤醒。
            # `unknown` 尤其重要 —— 它表示"既不能证明完成，也不能证明没发生"，
            # 据它唤醒等于让模型在未知副作用之上继续推进。
            still_pending_by_run[run_id] = still_pending_by_run.get(run_id, 0) + 1
            if state == str(JobState.UNKNOWN):
                note = f"{row.job_id}:unknown" + (f"（{problem}）" if problem else "")
                unresolved_by_run.setdefault(run_id, []).append(note)
                result.setdefault("unresolved", []).append(str(row.job_id))
            continue

        terminal_by_run.setdefault(run_id, []).append((row.job_id, state))
        result["terminal"].append(str(row.job_id))

    for run_id in waiting:
        remaining = still_pending_by_run.get(run_id, 0)
        if remaining:
            unresolved = unresolved_by_run.get(run_id, [])
            result["still_waiting"].append(str(run_id))
            if unresolved:
                result["notes"].append(
                    f"run {run_id} 仍有无法核实的任务，保持 waiting_tool，"
                    f"需要用户确认后才能继续：{unresolved[:5]}"
                )
            continue
        if open_wait_sets.get(run_id):
            result["still_waiting"].append(str(run_id))
            result["notes"].append(
                f"run {run_id} 的任务已终态，但仍有未满足的等待条件：{open_wait_sets[run_id][:3]}"
            )
            continue
        terminal_entries = terminal_by_run.get(run_id, [])
        summary = _terminal_summary([f"{job_id}:{state}" for job_id, state in terminal_entries])
        if await _wake_run(
            db, run_id, summary=summary, terminal_job_ids=[job_id for job_id, _ in terminal_entries]
        ):
            result["woken"].append(str(run_id))

    return result


def _no_pending_summary() -> str:
    return (
        "先前提交的后台任务已经没有待核对记录；请用 get_job / read_job_output "
        "确认结论后再继续推进。"
    )


def _terminal_summary(entries: list[str]) -> str:
    if not entries:
        return "后台任务已结束（无具体状态记录）。"
    return "后台任务已结束：" + "；".join(entries)


async def _wake_run(
    db: Database,
    run_id: UUID,
    *,
    summary: str,
    terminal_job_ids: list[UUID] | None = None,
    explicit_wait_satisfied: bool = False,
) -> bool:
    """把等待中的 run 重新排队，并注入一条系统通知。

    - 走 `set_run_status`，让状态机成为唯一真源；
    - 通知作为一条 **MESSAGE 指令**入队，由 `CommandGuardMiddleware` 在下一个
      一致边界注入 —— 既复用了既有的消息注入通路，也保证不会插进未闭合的
      assistant/tool 配对里；
    - **不**伪造对应原 tool_call 的 tool 消息（§8.3 明确禁止）；
    - 同时写事件，UI 与诊断都能看到"是谁唤醒了它、为什么"。
    """
    async with db.transaction() as session:
        run = (
            await session.execute(select(RunRow).where(RunRow.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if run is None or RunStatus(run.status) is not RunStatus.WAITING_TOOL:
            # 已经被别处唤醒或推进过：不做第二次，也不报告"唤醒成功"
            return False

        # 与投影同步的事务之间可能又有新 job 被登记。唤醒前必须再查一次，
        # 避免在未知副作用仍在运行时把 run 交回模型。
        pending = (
            await session.execute(
                select(ToolJobRow.job_id)
                .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                .outerjoin(
                    ExecutionResourceRow, ExecutionResourceRow.resource_id == ToolJobRow.job_id
                )
                .where(
                    (ExecutionResourceRow.resource_id.is_(None))
                    | (
                        ExecutionResourceRow.kind.in_(["command", "script"])
                        & (
                            ExecutionResourceRow.detail["completion_role"].astext.is_distinct_from(
                                "resource"
                            )
                        )
                    )
                )
                .where(
                    ToolOperationRow.run_id == run_id,
                    ToolJobRow.state.in_(list(UNRESOLVED_JOB_STATES)),
                )
                .limit(1)
            )
        ).first()
        # 明确 wait any 命中一个结果即可回到 Agent；其余有限工作仍由完成闸门核对。
        if pending is not None and not explicit_wait_satisfied:
            return False
        # 未满足的明确等待条件同样阻止唤醒：条件满足由事件周期标记。
        from agent_workbench.persistence.tables import ExecutionWaitSetRow

        open_wait = (
            await session.execute(
                select(ExecutionWaitSetRow.id)
                .where(
                    ExecutionWaitSetRow.run_id == run_id,
                    ExecutionWaitSetRow.status == "open",
                )
                .limit(1)
            )
        ).first()
        if open_wait is not None:
            return False

        stale_job_ids: list[str] = []
        version_unknown_job_ids: list[str] = []
        if terminal_job_ids:
            rows = (
                await session.execute(
                    select(
                        ToolOperationRow.job_id, ToolOperationRow.dispatch_control_version
                    ).where(
                        ToolOperationRow.run_id == run_id,
                        ToolOperationRow.job_id.in_(terminal_job_ids),
                    )
                )
            ).all()
            for job_id, dispatched_version in rows:
                if dispatched_version is None:
                    version_unknown_job_ids.append(str(job_id))
                elif int(dispatched_version) != int(run.control_version):
                    stale_job_ids.append(str(job_id))
        requires_revalidation = bool(stale_job_ids or version_unknown_job_ids)
        if stale_job_ids:
            summary += (
                f"；其中 {len(stale_job_ids)} 个任务属于旧方向"
                f"（当前版本 {run.control_version}）：{stale_job_ids[:5]}"
            )
        if version_unknown_job_ids:
            summary += (
                f"；其中 {len(version_unknown_job_ids)} 个历史任务缺少派发版本："
                f"{version_unknown_job_ids[:5]}"
            )

        # 幂等键按 (run, 摘要) 生成：重复巡检不会给模型塞第二份同样的通知
        digest = hashlib.sha256(f"{run_id}|{summary}".encode()).hexdigest()[:32]
        await CommandRepository(session).submit(
            session_id=run.session_id,
            run_id=run_id,
            kind=CommandKind.MESSAGE,
            content=wakeup_message(summary, requires_revalidation=requires_revalidation),
            idempotency_key=f"job-wakeup-{digest}",
            payload={"source": "job_wakeup", "reason": "jobs_reached_terminal"},
        )
        await set_run_status(session, run_id=run_id, target=RunStatus.QUEUED)
        await append_event(
            session,
            session_id=run.session_id,
            run_id=run_id,
            event_type=EventType.RUN_STATUS,
            payload={
                "status": str(RunStatus.QUEUED),
                "reason": "jobs_reached_terminal",
                "summary": summary,
                "stale_job_ids": stale_job_ids,
                "version_unknown_job_ids": version_unknown_job_ids,
                "note": "由后台任务唤醒器触发；等待期间没有调用模型。",
            },
        )
        await append_event(
            session,
            session_id=run.session_id,
            run_id=run_id,
            event_type=EventType.TOOL_COMPLETED,
            payload={
                "scope": "job_wakeup",
                "summary": summary,
                "stale_job_ids": stale_job_ids,
                "version_unknown_job_ids": version_unknown_job_ids,
                "injected_message": True,
            },
        )
    return True


def wakeup_message(summary: str, *, requires_revalidation: bool = False) -> str:
    """注入给模型的通知文本。

    刻意与用户消息区分开：它来自系统，用于告知"后台任务结果已就绪"。
    """
    caution = (
        "旧方向或版本未知的任务结果仅是历史执行记录，不能直接支撑当前结论。"
        "如需采用，请先按当前目标与范围重新核对，必要时显式发起新操作。\n"
        if requires_revalidation
        else ""
    )
    return (
        f"{WAKEUP_MARKER}\n"
        f"{summary}\n"
        f"{caution}"
        "请用 read_job_output 读取你需要的输出，并基于实际结果继续推进；"
        "不要重新提交同一个请求。"
    )


async def close_quietly(client: Any, tunnel: Any) -> None:
    for item in (client, tunnel):
        close = getattr(item, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            continue


def is_definite_terminal(state: str) -> bool:
    """是否已有**确定结论**。

    `unknown` 不算：它表示"无法核实"，据它唤醒 run 等于在未知副作用上继续。
    """
    return state in DEFINITE_TERMINAL_STATES


def is_unresolved(state: str) -> bool:
    return state in UNRESOLVED_JOB_STATES


def utcnow() -> datetime:
    return datetime.now(UTC)
