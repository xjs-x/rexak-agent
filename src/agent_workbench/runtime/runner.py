"""Agent Runner：运行所有权、图调用、事件驱动续跑。

边界（§6.4、§2）：
- Runner 只处理**运行所有权、等待唤醒和框架调用**，不重新承担逐步推理；
- 浏览器/SSE 断开**不停止任务**；
- 使用 checkpoint 与受控执行片段：达到片段工作阈值后在**没有悬空工具消息的边界**
  保存状态并调度下一片段；达到图步数上限进入恢复流程而**不是当成完成**；
- lease 心跳与耗时模型/工具调用**解耦**：不得因等待模型而忘记续租。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage
from openai import APIConnectionError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import AgentRuntime, open_agent_runtime
from agent_workbench.agent.signals import (
    FinishGateRejected,
    GraphRunAborted,
    LeaseLostError,
    NoProgressBlocked,
    SegmentBoundaryReached,
    StaleResponseError,
)
from agent_workbench.agent.state import assert_compatible_state, next_input_state
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import CompletionReason, ErrorCode, RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import (
    claim_run,
    is_current_owner,
    release_run,
    set_run_status,
)
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import (
    build_artifact_store,
    contains_secret_marker,
    redact_text,
)
from agent_workbench.execution.client import HttpExecutorClient
from agent_workbench.execution.tunnel import SshTunnel
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.db import Database, get_database
from agent_workbench.persistence.tables import RunRow, SessionRow
from agent_workbench.tools.registry import default_selection
from agent_workbench.tools.runtime import ResourceLocks, Toolbox

log = get_logger("agent_workbench.runner")

#: 单次 astream 调用的图步数上限。这是**片段内保护**，不是长期运行方案（§6.4）。
SEGMENT_RECURSION_LIMIT = 60

#: 流式增量块的合并窗口：避免每 token 一次事务（§12.2）
DELTA_FLUSH_INTERVAL_SECONDS = 0.25
DELTA_FLUSH_MAX_CHARS = 600
# 保留足够尾部，防止敏感字段名被拆在两次已发布分片之间。
DELTA_REDACTION_LOOKBEHIND = 32
DELTA_SENSITIVE_MAX_CHARS = 100_000


@dataclass(slots=True)
class RunOutcome:
    run_id: UUID
    status: RunStatus
    detail: str = ""
    completion_reason: CompletionReason | None = None
    segments: int = 0


@dataclass(slots=True)
class _DeltaBuffer:
    """短时间块缓冲；敏感字段未完整前不把其后文本写入事件。"""

    session_id: UUID
    run_id: UUID
    pending: str = ""
    holding_sensitive: bool = False
    hidden_overflow: bool = False
    last_flush: float = field(default_factory=time.monotonic)

    def add(self, text: str) -> str | None:
        if self.hidden_overflow:
            return None
        self.pending += text
        if contains_secret_marker(self.pending):
            self.holding_sensitive = True
        if self.holding_sensitive:
            if len(self.pending) > DELTA_SENSITIVE_MAX_CHARS:
                self.pending = ""
                self.hidden_overflow = True
                return "[实时文本含敏感字段且过长，后续内容已隐藏；请查看对话归档]"
            return None
        now = time.monotonic()
        if (
            len(self.pending) >= DELTA_FLUSH_MAX_CHARS
            or now - self.last_flush >= DELTA_FLUSH_INTERVAL_SECONDS
        ):
            if len(self.pending) <= DELTA_REDACTION_LOOKBEHIND:
                return None
            chunk, self.pending = (
                self.pending[:-DELTA_REDACTION_LOOKBEHIND],
                self.pending[-DELTA_REDACTION_LOOKBEHIND:],
            )
            self.last_flush = now
            return redact_text(chunk)
        return None

    def take(self) -> str | None:
        if self.hidden_overflow:
            return None
        if not self.pending:
            self.last_flush = time.monotonic()
            return None
        chunk, self.pending = self.pending, ""
        self.last_flush = time.monotonic()
        return redact_text(chunk)


class AgentRunner:
    """一个 Runner 进程实例。

    同一 session 同时最多一个运行所有者；第二个 Runner 因 lease 冲突拿不到执行权。
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        owner: str | None = None,
        database: Database | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        production_scan: bool = False,
    ) -> None:
        self._settings = settings or get_settings()
        self._owner = owner or f"runner-{self._settings.owner_id}"
        self._db = database or get_database()
        self._on_event = on_event
        self._stopping = False
        self._production_scan = production_scan

    @property
    def owner(self) -> str:
        return self._owner

    def request_stop(self) -> None:
        self._stopping = True

    # ------------------------------------------------------------------ #
    # 候选 run
    # ------------------------------------------------------------------ #

    async def _candidate_runs(
        self,
        *,
        limit: int = 5,
        only_run_id: UUID | None = None,
        exclude_cancelling: bool = False,
        only_cancelling: bool = False,
    ) -> list[RunRow]:
        """找出可以推进的 run。

        - `queued`：新阶段，直接可跑；
        - `paused` / `waiting_user` / `waiting_tool`：仅当有**待消费指令**时才继续，
          否则保持等待，避免空转（§4.2）。
        """
        async with self._db.session() as session:
            from sqlalchemy import case, exists

            from agent_workbench.runtime.selection import owned_run_ids

            interactive = (RunRow.config_snapshot["created_from"].as_string() == "message") | (
                RunRow.config_snapshot["created_from_session_create"].as_boolean().is_(True)
            )
            if self._production_scan and only_run_id is None:
                from agent_workbench.persistence.tables import CommandRow

                pending = exists(
                    select(CommandRow.id).where(
                        CommandRow.session_id == RunRow.session_id,
                        CommandRow.status == "queued",
                    )
                )
                enabled = RunRow.session_id.in_(
                    select(SessionRow.id).where(SessionRow.status != "deleted")
                )
                cancelling = RunRow.status == str(RunStatus.CANCELLING)
                resumable = (RunRow.status == str(RunStatus.PAUSING)) | (
                    RunRow.status.in_(
                        [
                            str(RunStatus.PAUSED),
                            str(RunStatus.WAITING_USER),
                            str(RunStatus.WAITING_TOOL),
                        ]
                    )
                    & pending
                )
                stmt = (
                    select(RunRow)
                    .where(
                        RunRow.id.in_(owned_run_ids(self._settings.owner_id)),
                        cancelling
                        | (enabled & ((RunRow.status == str(RunStatus.QUEUED)) | resumable)),
                    )
                    .order_by(
                        case((cancelling, 0), (pending, 1), (interactive, 2), else_=3),
                        RunRow.created_at.desc(),
                        RunRow.id,
                    )
                    .limit(limit)
                )
                if exclude_cancelling:
                    stmt = stmt.where(RunRow.status != str(RunStatus.CANCELLING))
                if only_cancelling:
                    stmt = stmt.where(RunRow.status == str(RunStatus.CANCELLING))
                return list((await session.execute(stmt)).scalars().all())
            queued_stmt = (
                select(RunRow)
                .where(RunRow.status == str(RunStatus.QUEUED))
                .order_by(RunRow.created_at)
                .limit(limit)
            )
            if self._production_scan:
                queued_stmt = (
                    queued_stmt.where(
                        RunRow.id.in_(owned_run_ids(self._settings.owner_id)),
                        RunRow.session_id.in_(
                            select(SessionRow.id).where(SessionRow.status != "deleted")
                        ),
                    )
                    .order_by(None)
                    .order_by(case((interactive, 0), else_=1), RunRow.created_at)
                )
            if only_run_id is not None:
                queued_stmt = select(RunRow).where(
                    RunRow.id == only_run_id,
                    RunRow.status == str(RunStatus.QUEUED),
                )
            queued = (await session.execute(queued_stmt)).scalars().all()
            resumable_stmt = (
                select(RunRow)
                .where(
                    RunRow.status.in_(
                        [
                            str(RunStatus.PAUSING),
                            str(RunStatus.PAUSED),
                            str(RunStatus.WAITING_USER),
                            str(RunStatus.WAITING_TOOL),
                        ]
                    )
                )
                .order_by(RunRow.updated_at)
                .limit(limit)
            )
            if self._production_scan:
                resumable_stmt = resumable_stmt.where(
                    RunRow.id.in_(owned_run_ids(self._settings.owner_id)),
                    RunRow.session_id.in_(
                        select(SessionRow.id).where(SessionRow.status != "deleted")
                    ),
                )
            if only_run_id is not None:
                resumable_stmt = select(RunRow).where(
                    RunRow.id == only_run_id,
                    RunRow.status.in_(
                        [
                            str(RunStatus.PAUSING),
                            str(RunStatus.PAUSED),
                            str(RunStatus.WAITING_USER),
                            str(RunStatus.WAITING_TOOL),
                        ]
                    ),
                )
            resumable = (await session.execute(resumable_stmt)).scalars().all()

            from agent_workbench.persistence.repositories import CommandRepository

            # 去重：`queued` 与 `resumable` 两个分支可能命中同一个 run
            # （例如刚被唤醒的 run 同时是 queued 又有待消费指令）。
            # 不去重会让 `step()` 对同一个 run 连续驱动两次。
            candidates: list[RunRow] = []
            seen: set[UUID] = set()
            cancelling_stmt = select(RunRow).where(RunRow.status == str(RunStatus.CANCELLING))
            if self._production_scan:
                cancelling_stmt = cancelling_stmt.where(
                    RunRow.id.in_(owned_run_ids(self._settings.owner_id))
                )
            if only_run_id is not None:
                cancelling_stmt = cancelling_stmt.where(RunRow.id == only_run_id)
            cancelling = (
                (await session.execute(cancelling_stmt.order_by(RunRow.updated_at).limit(limit)))
                .scalars()
                .all()
            )
            for run in list(cancelling) + list(queued):
                if run.id in seen:
                    continue
                seen.add(run.id)
                candidates.append(run)
            for run in resumable:
                if run.id in seen:
                    continue
                repo = CommandRepository(session)
                if RunStatus(run.status) is RunStatus.PAUSING or await repo.list_pending(
                    run.session_id
                ):
                    seen.add(run.id)
                    candidates.append(run)
            return candidates[:limit]

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    async def step(self, *, only_run_id: UUID | None = None) -> RunOutcome | None:
        """推进一个 run（如果有）。返回 None 表示当前没有可推进的工作。

        `only_run_id` 用于**定向驱动**某个 run（评测/调试）；
        不传则按常规候选扫描。默认路径下单个 run 的异常不会中断整轮调度。
        """
        candidates = await self._candidate_runs(only_run_id=only_run_id)
        if not candidates:
            return None
        for run in candidates:
            try:
                outcome = await self._drive(run.id)
            except Exception as exc:
                if isinstance(exc, OperationalError):
                    # 数据库失联时既无法可靠写失败状态，也不能判断工具副作用。
                    # 保留运行租约，待过期后按恢复流程核对远端状态。
                    log.error(
                        "run_database_unavailable",
                        run_id=str(run.id),
                        error_type=type(exc).__name__,
                    )
                    raise
                # 单个 run 的异常**不能**中断整轮调度，否则一个坏 run 会把
                # 后面所有 run 一起堵死（这是联调时真实发生过的故障）。
                log.error(
                    "run_drive_failed",
                    run_id=str(run.id),
                    error=f"{type(exc).__name__}: {exc}",
                )
                await self._fail(
                    run.id,
                    ErrorCode.INTERNAL_ERROR,
                    f"Runner 驱动该 run 时异常终止：{type(exc).__name__}: {exc}",
                )
                try:
                    async with self._db.transaction() as session:
                        await set_run_status(
                            session, run_id=run.id, target=RunStatus.FAILED, stop_reason=str(exc)
                        )
                except Exception:
                    log.error("run_fail_marking_failed", run_id=str(run.id))
                return RunOutcome(run_id=run.id, status=RunStatus.FAILED, detail=str(exc))
            if outcome is not None:
                return outcome
        return None

    async def serve(self, *, poll_interval: float = 1.0, only_run_id: UUID | None = None) -> None:
        """常驻循环。SSE/浏览器断开不影响它。

        每一轮先做一次**后台任务唤醒巡检**，再找一个 run 推进。
        巡检在"没有等待中的 run"时只是一次廉价查询，不会触碰执行岛；
        有等待中的 run 时按 `job_wakeup_interval_seconds` 节流。
        **等待期间不调用模型**（§8.3 "等待工具期间模型空转"）。
        """
        if only_run_id is None:
            self._production_scan = True
            await self._serve_sessions(poll_interval=poll_interval)
            return

        from agent_workbench.runtime.jobs import probe_and_wake

        await self._db.start()
        last_probe = 0.0
        try:
            while not self._stopping:
                outcome = None
                try:
                    now = time.monotonic()
                    if now - last_probe >= self._settings.job_wakeup_interval_seconds:
                        last_probe = now
                        wake = await probe_and_wake(
                            self._db,
                            settings=self._settings,
                            run_ids=[only_run_id] if only_run_id is not None else None,
                        )
                        if wake["woken"]:
                            log.info("jobs_woken", **wake)
                    outcome = await self.step(only_run_id=only_run_id)
                except Exception as exc:
                    log.error("runner_loop_failed", error=f"{type(exc).__name__}: {exc}")
                    outcome = None
                if outcome is None:
                    await asyncio.sleep(poll_interval)
        finally:
            await self._db.close()

    async def _serve_sessions(self, *, poll_interval: float) -> None:
        """调度不同会话；每个会话仍由同一个官方 Agent 图驱动。"""
        from agent_workbench.runtime.jobs import probe_and_wake
        from agent_workbench.runtime.selection import owned_run_ids

        tasks: dict[UUID, asyncio.Task[RunOutcome | None]] = {}
        cancellation_task: asyncio.Task[RunOutcome | None] | None = None
        await self._db.start()
        last_probe = 0.0
        try:
            while not self._stopping:
                if cancellation_task is not None and cancellation_task.done():
                    try:
                        cancellation_task.result()
                    except Exception as exc:
                        log.error("cancel_worker_failed", error_type=type(exc).__name__)
                    cancellation_task = None
                for run_id, task in list(tasks.items()):
                    if task.done():
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            log.error(
                                "run_worker_failed",
                                run_id=str(run_id),
                                error_type=type(exc).__name__,
                            )
                        del tasks[run_id]
                try:
                    if tasks:
                        for run_id in await self._pausing_runs(list(tasks)):
                            if not tasks[run_id].cancelling():
                                tasks[run_id].cancel()
                    if cancellation_task is None:
                        candidates = await self._candidate_runs(limit=1, only_cancelling=True)
                        if candidates:
                            run_id = candidates[0].id
                            if run_id in tasks:
                                if not tasks[run_id].cancelling():
                                    tasks[run_id].cancel()
                            else:
                                cancellation_task = asyncio.create_task(
                                    self.step(only_run_id=run_id)
                                )
                    now = time.monotonic()
                    if now - last_probe >= self._settings.job_wakeup_interval_seconds:
                        last_probe = now
                        async with self._db.session() as db:
                            waiting = list(
                                (
                                    await db.execute(
                                        select(RunRow.id).where(
                                            RunRow.id.in_(owned_run_ids(self._settings.owner_id)),
                                            RunRow.status == str(RunStatus.WAITING_TOOL),
                                        )
                                    )
                                )
                                .scalars()
                                .all()
                            )
                        if waiting:
                            await probe_and_wake(self._db, settings=self._settings, run_ids=waiting)
                    free = self._settings.runner_max_concurrent_runs - len(tasks)
                    if free > 0:
                        for run in await self._candidate_runs(
                            limit=max(5, len(tasks) + free), exclude_cancelling=True
                        ):
                            if run.id in tasks:
                                continue
                            tasks[run.id] = asyncio.create_task(self.step(only_run_id=run.id))
                            free -= 1
                            if free == 0:
                                break
                except Exception as exc:
                    log.error("runner_loop_failed", error_type=type(exc).__name__)
                await asyncio.sleep(poll_interval)
        finally:
            if cancellation_task is not None:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            await self._db.close()

    # ------------------------------------------------------------------ #
    # 驱动一个 run
    # ------------------------------------------------------------------ #

    async def _pausing_runs(self, run_ids: list[UUID]) -> list[UUID]:
        async with self._db.session() as db:
            return list(
                (
                    await db.execute(
                        select(RunRow.id).where(
                            RunRow.id.in_(run_ids), RunRow.status == str(RunStatus.PAUSING)
                        )
                    )
                ).scalars()
            )

    async def _drive(self, run_id: UUID) -> RunOutcome | None:
        # 1) 取所有权
        async with self._db.transaction() as session:
            acquired, generation = await claim_run(
                session,
                run_id=run_id,
                owner=self._owner,
                lease_seconds=self._settings.run_lease_seconds,
            )
        if not acquired:
            log.info("run_lease_held_by_other", run_id=str(run_id))
            return None

        async with self._db.session() as session:
            run = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
            session_row = (
                await session.execute(select(SessionRow).where(SessionRow.id == run.session_id))
            ).scalar_one()

        heartbeat = asyncio.create_task(self._heartbeat(run_id))
        try:
            if RunStatus(run.status) is RunStatus.PAUSING:
                await self._finalize(
                    run.id,
                    RunStatus.PAUSED,
                    detail="已暂停；发送新指令或点击继续即可恢复",
                    completion_reason=None,
                )
                return RunOutcome(run_id=run.id, status=RunStatus.PAUSED)
            if RunStatus(run.status) is RunStatus.CANCELLING:
                confirmed = await self._cancel_active_jobs(run)
                target = RunStatus.CANCELLED if confirmed else RunStatus.CANCELLING
                detail = "已确认取消" if confirmed else "取消请求尚未确认，继续核对远端状态"
                await self._finalize(run.id, target, detail=detail, completion_reason=None)
                if not confirmed:
                    await asyncio.sleep(self._settings.job_wakeup_interval_seconds)
                return RunOutcome(run_id=run.id, status=target, detail=detail)
            return await self._run_segments(run, session_row, generation)
        except asyncio.CancelledError:
            # 只有用户暂停才落 paused。进程退出/取消任务仍交给原有恢复与取消链路。
            async with self._db.session() as db:
                current = await db.get(RunRow, run_id)
            if current is None or RunStatus(current.status) is not RunStatus.PAUSING:
                raise
            await self._finalize(
                run_id,
                RunStatus.PAUSED,
                detail="已停止当前模型等待；发送新指令或点击继续即可恢复",
                completion_reason=None,
            )
            return RunOutcome(run_id=run_id, status=RunStatus.PAUSED)
        finally:
            heartbeat.cancel()
            # 用 gather 吸收取消/异常；直接 await task.result() 在任务尚未真正启动时
            # 会抛 InvalidStateError，掩盖真正的失败原因。
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, run_id: UUID) -> None:
        """独立续租。**与模型/工具耗时解耦**（§4.3）。"""
        interval = max(2, self._settings.lease_heartbeat_seconds)
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._db.transaction() as session:
                    acquired, _ = await claim_run(
                        session,
                        run_id=run_id,
                        owner=self._owner,
                        lease_seconds=self._settings.run_lease_seconds,
                    )
                if not acquired:
                    log.warning("lease_lost_during_run", run_id=str(run_id))
                    return
        except asyncio.CancelledError:
            raise

    async def _run_segments(
        self, run: RunRow, session_row: SessionRow, generation: int
    ) -> RunOutcome:
        settings = self._settings
        from agent_workbench.domain.instruction_scope import sync_instruction_scope

        async with self._db.transaction() as db:
            scope_snapshot = await sync_instruction_scope(
                db, session_id=session_row.id, run_id=run.id
            )
        scope = ScopePolicy(scope_snapshot)
        tunnel = SshTunnel(settings)
        executor = HttpExecutorClient(
            tunnel, settings=settings, expected_executor_id=settings.executor_id
        )
        artifacts = build_artifact_store()
        spans = SpanRecorder()
        selection = default_selection(settings)

        # ------------------------------------------------------------------ #
        # 任务开始时预检：缺少必要条件时**明确落终态**，不要开跑后才发现。
        # 这里的失败属于基础设施/配置故障，必须在事件里写清楚
        # "不是目标网站的阴性安全结论"（§4.2、§6.5 第 6 条）。
        # ------------------------------------------------------------------ #
        blocked = self._precheck_configuration(run)
        if blocked is not None:
            code, message = blocked
            await self._set_status(run.id, RunStatus.RUNNING, reason="runner_acquired")
            await self._fail(run.id, code, message)
            await self._finalize(run.id, RunStatus.FAILED, detail=message, completion_reason=None)
            return RunOutcome(run_id=run.id, status=RunStatus.FAILED, detail=message)

        await self._set_status(run.id, RunStatus.RUNNING, reason="runner_acquired")

        try:
            await executor.start()
            # 能力预检：协议版本不兼容时给出明确阻碍，不默认远端一定支持（§8.6）
            await executor.capabilities()
        except Exception as exc:
            detail = f"执行岛不可用：{type(exc).__name__}: {exc}"
            await self._fail(run.id, ErrorCode.EXECUTOR_UNAVAILABLE, detail)
            await self._finalize(run.id, RunStatus.FAILED, detail=detail, completion_reason=None)
            await self._shutdown(tunnel, executor)
            return RunOutcome(run_id=run.id, status=RunStatus.FAILED, detail=detail)

        toolbox = Toolbox(
            run_id=run.id,
            session_id=run.session_id,
            owner=self._owner,
            owner_generation=generation,
            settings=settings,
            db=self._db,
            scope=scope,
            executor=executor,
            artifacts=artifacts,
            spans=spans,
            locks=ResourceLocks(settings),
            provider_id=run.provider_id,
            model_name=run.model_name,
            sdk_retries_disabled=True,
            policy_revision=int((session_row.policy_snapshot or {}).get("revision", 1)),
            scope_revision=int((session_row.scope_snapshot or {}).get("revision", 1)),
            identity_id=session_row.active_identity_id,
            interactive_threshold_seconds=float(settings.tool_interactive_threshold_seconds),
            control_check=self._assert_dispatch_allowed,
        )
        context = RunContext(toolbox=toolbox, thread_id=session_row.thread_id)

        segments = 0
        status = RunStatus.RUNNING
        detail = ""
        completion_reason: CompletionReason | None = None

        try:
            from agent_workbench.domain.prices import price_catalog_for_run

            async with self._db.transaction() as db:
                price_catalog = await price_catalog_for_run(db, run.id)
            runtime_ctx = open_agent_runtime(
                settings=settings, selection=selection, price_catalog=price_catalog
            )
            runtime = await runtime_ctx.__aenter__()
        except Exception as exc:
            detail = (
                f"Agent 运行时装配失败：{type(exc).__name__}: {exc}。"
                "这是配置/基础设施问题，不是目标网站的阴性结论。"
            )
            await self._fail(run.id, ErrorCode.INTERNAL_ERROR, detail)
            await self._finalize(run.id, RunStatus.FAILED, detail=detail, completion_reason=None)
            await self._shutdown(tunnel, executor)
            return RunOutcome(run_id=run.id, status=RunStatus.FAILED, detail=detail)

        try:
            while True:
                segments += 1
                try:
                    payload = await self._segment_input(runtime, run, session_row, segments)
                    await self._stream_once(runtime, context, toolbox, session_row, run, payload)
                    if segments == 1:
                        await self._acknowledge_input_boundary(run)
                    status_final, reason = await self._resolve_after_segment(
                        runtime, run, session_row, spans
                    )
                    if status_final is not None:
                        status = status_final
                        completion_reason = reason
                        break
                    if await self._segment_should_continue(run):
                        continue

                    # 还有非终态后台任务 -> 进入 waiting_tool 并**返回**，
                    # 由唤醒器在任务到终态后重新排队。这里绝不继续调模型
                    # （否则就是"等待工具期间模型空转"，§8.3）。
                    from agent_workbench.runtime.jobs import pending_jobs_for_run

                    pending = await pending_jobs_for_run(self._db, run.id)
                    if pending:
                        status = RunStatus.WAITING_TOOL
                        detail = (
                            f"等待 {len(pending)} 个后台任务到达终态；"
                            "等待期间不调用模型，由任务唤醒器触发续跑。"
                        )
                        await self._emit(
                            run,
                            EventType.RUN_STATUS,
                            {
                                "status": str(RunStatus.WAITING_TOOL),
                                "pending_jobs": [str(item.job_id) for item in pending],
                                "note": detail,
                            },
                        )
                        break

                    status = RunStatus.WAITING_USER
                    detail = "本片段结束，等待新的输入或事件"
                    break
                except SegmentBoundaryReached as exc:
                    detail = str(exc)
                    log.info("segment_boundary", run_id=str(run.id), segments=segments)
                    continue
                except GraphRunAborted as exc:
                    status, completion_reason, detail = await self._handle_abort(run, exc)
                    break
                except LeaseLostError as exc:
                    status, detail = RunStatus.FAILED, str(exc)
                    break
                except FinishGateRejected as exc:
                    detail = "; ".join(exc.feedback)
                    status = RunStatus.WAITING_USER
                    break
                except NoProgressBlocked as exc:
                    from agent_workbench.runtime.jobs import pending_jobs_for_run

                    pending = await pending_jobs_for_run(self._db, run.id)
                    if pending:
                        status = RunStatus.WAITING_TOOL
                        detail = f"{exc}；仍有 {len(pending)} 个后台任务，等待结果后再评估。"
                    else:
                        status = RunStatus.WAITING_USER
                        detail = (
                            f"{exc}；已暂停重复探索，等待用户提供新方向。"
                            "这不是目标安全或测试穷尽的结论。"
                        )
                    break
                except Exception as exc:
                    status, detail = await self._classify_exception(run.id, exc)
                    break

        finally:
            # checkpointer / store 连接成对关闭（生命周期由本函数持有）
            try:
                with contextlib.suppress(Exception):
                    await runtime_ctx.__aexit__(None, None, None)
            finally:
                await self._shutdown(tunnel, executor)

        await self._finalize(run.id, status, detail=detail, completion_reason=completion_reason)
        if status is RunStatus.COMPLETED:
            try:
                from agent_workbench.memory.extract import distill_completed_run

                count = await distill_completed_run(self._db, settings, run.id)
                log.info("run_memory_distilled", run_id=str(run.id), candidates=count)
            except Exception as exc:
                # 记忆提炼是阶段完成后的派生投影；失败不能改写已提交的 run 结论。
                log.error(
                    "run_memory_distillation_failed",
                    run_id=str(run.id),
                    error=f"{type(exc).__name__}: {exc}",
                )
        return RunOutcome(
            run_id=run.id,
            status=status,
            detail=detail,
            completion_reason=completion_reason,
            segments=segments,
        )

    def _precheck_configuration(self, run: RunRow) -> tuple[ErrorCode, str] | None:
        """开跑前的必要条件预检。

        缺模型凭据属于**部署/配置故障**，不是"目标网站安全"。
        这类问题必须立刻落终态并给出可操作的原因，而不是让 `create_agent` 装配时
        在深层抛 `SecretNotFoundError`（那会表现为"Runner 卡住"，联调时真实发生过）。
        """
        from agent_workbench.config.secrets import (
            SecretNotFoundError,
            build_secret_store,
        )
        from agent_workbench.models.provider import (
            ProviderConfigurationError,
            resolve_provider_profile,
        )

        try:
            profile = resolve_provider_profile(run.provider_id, run.model_name, self._settings)
        except ProviderConfigurationError as exc:
            return (
                ErrorCode.PROVIDER_ERROR,
                f"供应商配置无效：{exc}。这是配置问题，不是目标网站的阴性结论。",
            )

        try:
            build_secret_store().resolve(profile.secret_ref)
        except SecretNotFoundError as exc:
            return (
                ErrorCode.PROVIDER_ERROR,
                f"模型凭据未配置：{exc}。请在环境变量或本机密钥存储中提供，"
                "然后重新提交任务。这不是目标网站的阴性结论。",
            )
        return None

    async def _shutdown(
        self, tunnel: SshTunnel, executor: HttpExecutorClient | None = None
    ) -> None:
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.close()
        with contextlib.suppress(Exception):
            await tunnel.close()

    # ------------------------------------------------------------------ #
    # 片段输入
    # ------------------------------------------------------------------ #

    async def _segment_input(
        self,
        runtime: AgentRuntime,
        run: RunRow,
        session_row: SessionRow,
        segment_index: int,
    ) -> Any:
        """决定这一片段喂给图什么。

        - checkpoint 仍有待执行节点 -> 用 `None` 恢复；
        - checkpoint 已到 END -> 增量输入重新进入官方图，保留历史，
          新指令由 CommandGuard 消费一次；
        - 首次输入 -> 组装最小输入（目标 + 用户原文）。
        """
        config = {"configurable": {"thread_id": session_row.thread_id}}
        try:
            snapshot = await runtime.graph.aget_state(config)
            values = (snapshot.values or {}) if snapshot is not None else {}
        except Exception as exc:
            log.error("checkpoint_read_failed", run_id=str(run.id), error_type=type(exc).__name__)
            raise RuntimeError(
                "checkpoint 读取失败，拒绝重复注入目标；请检查数据库连接与迁移版本"
            ) from exc

        assert_compatible_state(values)

        config_snapshot = getattr(run, "config_snapshot", None) or {}
        marker = config_snapshot.get("restart_checkpoint_on_input")
        replace_old_run = values.get("active_run_id") not in {None, str(run.id)}
        closures = []
        if values.get("messages") and segment_index == 1 and (marker or replace_old_run):
            from agent_workbench.agent.checkpoint_boundary import (
                finish_interrupted_checkpoint,
                unresolved_tool_messages,
            )

            if getattr(snapshot, "next", ()):
                values, closures = await finish_interrupted_checkpoint(runtime.graph, config)
            else:
                closures = unresolved_tool_messages(values)
            # 即使旧图处于 END，新输入也要重置旧的结束/无进展判定。
            objective = run.objective
            if marker:
                from agent_workbench.persistence.tables import CommandRow

                async with self._db.session() as db:
                    command = await db.get(CommandRow, UUID(str(marker)))
                if command is not None:
                    objective = command.content or str(command.payload.get("content") or objective)
            payload = next_input_state(
                session_id=run.session_id,
                run_id=run.id,
                objective=objective,
                user_message=objective,
                previous_plan=values.get("plan"),
            )
            payload["messages"] = closures
            if not marker and config_snapshot.get("created_from") != "message":
                from langchain_core.messages import HumanMessage

                payload["messages"] = [
                    *payload["messages"],
                    HumanMessage(content=objective, id=f"objective-{run.id}"),
                ]
            return payload

        if values.get("messages"):
            if getattr(snapshot, "next", ()):
                if values.get("active_run_id") not in {None, str(run.id)}:
                    raise RuntimeError("旧阶段的图仍有待执行节点，拒绝在新阶段重放旧工具")
                return None
            # 图已到 END：None 不会重新进入模型节点。显式增量输入重新进入
            # 官方图；用户指令仍由 CommandGuard 消费，既保留历史也不重放工具。
            if values.get("active_run_id") == str(run.id):
                return {
                    "messages": [],
                    "completion_proposal": None,
                    "finish_gate_feedback": [],
                    "finish_gate_attempts": 0,
                    "no_progress_streak": 0,
                    "last_progress_signature": "",
                    "segment_work_units": 0,
                }
            payload = next_input_state(
                session_id=run.session_id,
                run_id=run.id,
                objective=run.objective,
                user_message=run.objective,
                previous_plan=values.get("plan"),
            )
            if (getattr(run, "config_snapshot", None) or {}).get("created_from") == "message":
                payload["messages"] = []
            return payload
        if segment_index > 1:
            # 后续片段却没有历史：说明状态读取异常，明确报错而不是重复注入目标
            raise RuntimeError(
                f"第 {segment_index} 个片段找不到线程历史，拒绝重复注入初始目标以免重复执行操作"
            )
        payload = next_input_state(
            session_id=run.session_id,
            run_id=run.id,
            objective=run.objective,
            user_message=run.objective,
        )
        if (getattr(run, "config_snapshot", None) or {}).get("created_from") == "message":
            payload["messages"] = []
        return payload

    async def _acknowledge_input_boundary(self, run: RunRow) -> None:
        marker = run.config_snapshot.get("restart_checkpoint_on_input")
        if not marker:
            return
        async with self._db.transaction() as db:
            row = (
                await db.execute(select(RunRow).where(RunRow.id == run.id).with_for_update())
            ).scalar_one()
            if row.config_snapshot.get("restart_checkpoint_on_input") == marker:
                updated = dict(row.config_snapshot)
                updated.pop("restart_checkpoint_on_input", None)
                row.config_snapshot = updated

    # ------------------------------------------------------------------ #
    # 流式执行
    # ------------------------------------------------------------------ #

    async def _stream_once(
        self,
        runtime: AgentRuntime,
        context: RunContext,
        toolbox: Toolbox,
        session_row: SessionRow,
        run: RunRow,
        payload: Any,
    ) -> None:
        config = runtime.config(
            thread_id=session_row.thread_id,
            run_id=str(run.id),
            recursion_limit=SEGMENT_RECURSION_LIMIT,
        )
        buffer = _DeltaBuffer(session_id=run.session_id, run_id=run.id)
        response_id: str | None = None
        received_chunk = False
        last_model_progress: tuple[str, str] | None = None
        last_model_progress_at = 0.0

        async for mode, chunk in runtime.graph.astream(
            payload,
            config=config,
            context=context,
            stream_mode=["messages", "updates"],
        ):
            received_chunk = True
            if mode == "messages":
                context.model_stream.observe(chunk)
                model_call = context.model_stream.active_call_id
                model_phase = context.model_stream.activity_phase
                progress = (model_call, model_phase) if model_call else None
                if (
                    progress
                    and model_phase != "waiting"
                    and (
                        progress != last_model_progress
                        or time.monotonic() - last_model_progress_at >= 10
                    )
                ):
                    await self._emit(
                        run,
                        EventType.RUN_STATUS,
                        {
                            "scope": "model_request",
                            "phase": model_phase,
                            "model_call_id": model_call,
                        },
                    )
                    last_model_progress = progress
                    last_model_progress_at = time.monotonic()
                text = _delta_text(chunk)
                if text:
                    message_id = getattr(chunk[0], "id", None)
                    if message_id and message_id != response_id:
                        tail = buffer.take()
                        if tail:
                            await self._emit_delta(run, tail, response_id=response_id)
                        response_id = str(message_id)
                    merged = buffer.add(text)
                    if merged:
                        await self._emit_delta(run, merged, response_id=response_id)
            elif mode == "updates":
                await self._handle_update(run, toolbox, chunk)

            # 每个块之后检查一次控制指令（暂停/取消/所有权）
            if toolbox.work_units and toolbox.work_units % 5 == 0:
                await self._assert_dispatch_allowed(run.id, self._owner, toolbox.owner_generation)

        if not received_chunk:
            raise RuntimeError(
                "Agent 图未执行任何节点；已停止空转，请检查调用配置与 checkpoint 状态"
            )

        tail = buffer.take()
        if tail:
            await self._emit_delta(run, tail, response_id=response_id)

    async def _handle_update(self, run: RunRow, toolbox: Toolbox, chunk: dict[str, Any]) -> None:
        """把图节点更新投影为事件。"""
        for node, update in (chunk or {}).items():
            if not isinstance(update, dict):
                continue
            plan = update.get("plan")
            if plan is not None:
                await self._emit(
                    run,
                    EventType.PLAN_UPDATED,
                    {
                        "node": node,
                        "plan_revision": update.get("plan_revision"),
                        "steps": [
                            {
                                "step_id": step.get("step_id"),
                                "description": step.get("description"),
                                "status": step.get("status"),
                                "rationale": step.get("rationale"),
                            }
                            for step in plan[:20]
                        ],
                    },
                )
            if update.get("completion_proposal") is not None:
                await self._emit(
                    run,
                    EventType.RUN_STATUS,
                    {
                        "event": "completion_proposed",
                        "proposal": update["completion_proposal"],
                    },
                )
        _ = toolbox

    async def _emit_delta(self, run: RunRow, text: str, *, response_id: str | None = None) -> None:
        await self._emit(
            run,
            EventType.MESSAGE_DELTA,
            {
                "role": "assistant",
                "delta": text,
                "response_id": response_id,
                "redaction_version": 1,
            },
        )

    async def _emit(self, run: RunRow, event_type: EventType, payload: dict[str, Any]) -> None:
        """持久化事件并在**提交之后**推送。"""
        envelope: dict[str, Any] | None = None
        try:
            async with self._db.transaction() as session:
                event = await append_event(
                    session,
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type=event_type,
                    payload=payload,
                )
            envelope = event.model_dump(mode="json")
        except Exception as exc:
            log.warning("event_persist_failed", error=f"{type(exc).__name__}: {exc}")
            return
        if self._on_event is not None and envelope is not None:
            with contextlib.suppress(Exception):
                self._on_event(envelope)

    # ------------------------------------------------------------------ #
    # 片段结束判定
    # ------------------------------------------------------------------ #

    async def _resolve_after_segment(
        self,
        runtime: AgentRuntime,
        run: RunRow,
        session_row: SessionRow,
        spans: SpanRecorder,
    ) -> tuple[RunStatus | None, CompletionReason | None]:
        """读取图状态，判断 run 是否已经该结束。"""
        try:
            snapshot = await runtime.graph.aget_state(
                {"configurable": {"thread_id": session_row.thread_id}}
            )
        except Exception as exc:
            log.warning("state_read_failed", error=f"{type(exc).__name__}: {exc}")
            return None, None

        values = (snapshot.values or {}) if snapshot is not None else {}
        proposal = values.get("completion_proposal")
        if not proposal:
            return None, None

        from agent_workbench.runtime.stopping import FinishGate

        span = spans.start(
            "finish_gate.runner",
            session_id=run.session_id,
            run_id=run.id,
        )
        try:
            async with self._db.session() as session:
                decision = await FinishGate(session).evaluate(
                    session_id=run.session_id, proposal=proposal
                )
            span.finish()
            spans.record(span, decision=decision.decision)
        except Exception as exc:
            span.finish(error=type(exc).__name__)
            raise
        finally:
            try:
                async with self._db.transaction() as session:
                    await spans.flush(session)
            except Exception as exc:
                log.warning(
                    "finish_gate_span_persist_failed",
                    run_id=str(run.id),
                    error_type=type(exc).__name__,
                )
        if not decision.allowed:
            return None, None

        reason_raw = str(proposal.get("reason") or "")
        reason = (
            CompletionReason.GOAL_ANSWERED
            if reason_raw == str(CompletionReason.GOAL_ANSWERED)
            else CompletionReason.EXPLORATION_CONVERGED
        )
        return RunStatus.COMPLETED, reason

    async def _segment_should_continue(self, run: RunRow) -> bool:
        """是否还有可以立即推进的工作（新的待消费指令）。"""
        from agent_workbench.persistence.repositories import CommandRepository

        async with self._db.session() as session:
            pending = await CommandRepository(session).list_pending(run.session_id)
        return bool(pending)

    # ------------------------------------------------------------------ #
    # 中断与异常
    # ------------------------------------------------------------------ #

    async def _handle_abort(
        self, run: RunRow, exc: GraphRunAborted
    ) -> tuple[RunStatus, CompletionReason | None, str]:
        if exc.reason == "cancelled":
            confirmed = await self._cancel_active_jobs(run)
            return (
                RunStatus.CANCELLED if confirmed else RunStatus.CANCELLING,
                None,
                exc.detail or ("已按用户请求取消" if confirmed else "远端取消尚未确认"),
            )
        return RunStatus.PAUSED, None, exc.detail or "已按用户请求暂停"

    async def _cancel_active_jobs(self, run: RunRow) -> bool:
        """取消全部活动工具并**核对结果**。

        不能在只发出取消请求时显示"已全部停止"（§4.2）。
        """
        from agent_workbench.persistence.repositories import ToolJobRepository

        async with self._db.session() as session:
            tracked = await ToolJobRepository(session).list_for_run(run.id)
            jobs = [
                job
                for job in tracked
                if job.state not in {"succeeded", "failed", "cancelled"}
                or job.sync_status == "uncertain"
                or (job.cancel_requested_at is not None and not job.cancel_confirmed)
            ]
        if not jobs:
            return True
        confirmed = True
        tunnel = SshTunnel(self._settings)
        client = HttpExecutorClient(
            tunnel, settings=self._settings, expected_executor_id=self._settings.executor_id
        )
        try:
            try:
                await client.start()
            except Exception as exc:
                await self._emit(
                    run,
                    EventType.ERROR,
                    {
                        "scope": "cancel",
                        "state": "unknown",
                        "message": f"取消连接未能确认：{type(exc).__name__}",
                    },
                )
                return False
            for job in jobs:
                import uuid as _uuid

                try:
                    receipt = await client.cancel(
                        job.job_id, _uuid.uuid5(run.id, f"cancel:{job.job_id}")
                    )
                except Exception as exc:
                    confirmed = False
                    await self._emit(
                        run,
                        EventType.ERROR,
                        {
                            "scope": "cancel",
                            "job_id": str(job.job_id),
                            "message": f"取消未能确认：{type(exc).__name__}: {exc}",
                            "state": "unknown",
                        },
                    )
                    continue
                confirmed = confirmed and receipt.process_confirmed_exited
                async with self._db.transaction() as session:
                    await ToolJobRepository(session).upsert(
                        job_id=job.job_id,
                        operation_id=job.operation_id,
                        executor_id=job.executor_id,
                        state=str(receipt.state),
                        cancel_requested=True,
                        cancel_confirmed=receipt.process_confirmed_exited,
                        sync_status="in_sync" if receipt.process_confirmed_exited else "uncertain",
                    )
                await self._emit(
                    run,
                    EventType.TOOL_COMPLETED,
                    {
                        "job_id": str(job.job_id),
                        "state": str(receipt.state),
                        "process_confirmed_exited": receipt.process_confirmed_exited,
                    },
                )
        finally:
            with contextlib.suppress(Exception):
                await client.close()
            with contextlib.suppress(Exception):
                await tunnel.close()
        return confirmed

    async def _classify_exception(self, run_id: UUID, exc: BaseException) -> tuple[RunStatus, str]:
        """区分"图步数上限"、"上下文溢出"和其它故障。

        **不把上限耗尽当成任务完成**（§6.4）。
        """
        name = type(exc).__name__
        message = str(exc)
        from agent_workbench.agent.middleware.compaction import CompactionFailed
        from agent_workbench.agent.middleware.model_observer import ModelRequestTimeout

        if isinstance(exc, ModelRequestTimeout):
            return RunStatus.RECOVERING, str(exc)
        if isinstance(exc, APIConnectionError):
            return (
                RunStatus.RECOVERING,
                f"模型服务连接中断：{type(exc).__name__}。本次用量可能不完整；"
                "未自动重发模型请求，核对运行状态后再继续。",
            )
        if isinstance(exc, OperationalError):
            return (
                RunStatus.RECOVERING,
                "数据库连接中断；保留 checkpoint 与远端操作记录，核对真实状态后再继续。"
                "这不是目标测试结论。",
            )
        if isinstance(exc, CompactionFailed):
            await self._fail(run_id, ErrorCode.INTERNAL_ERROR, message)
            return RunStatus.RECOVERING, message
        if "recursion" in message.lower() or "GraphRecursionError" in name:
            detail = (
                "达到片段内的图步数上限。这不是任务完成，也不是失败："
                "checkpoint 已保存，下一次片段会从该点继续。"
            )
            return RunStatus.RECOVERING, detail
        if "context" in message.lower() and (
            "overflow" in message.lower() or "too long" in message
        ):
            await self._fail(run_id, ErrorCode.CONTEXT_OVERFLOW, message)
            return RunStatus.FAILED, f"上下文超出模型容量：{message}"
        await self._fail(run_id, ErrorCode.INTERNAL_ERROR, f"{name}: {message}")
        return RunStatus.FAILED, f"{name}: {message}"

    async def _assert_dispatch_allowed(
        self,
        run_id: UUID,
        owner: str,
        generation: int,
        pinned_control_version: int | None = None,
    ) -> None:
        """派发前检查（由工具通过 `control_check` 调用）。

        - 所有权丢失 -> `LeaseLostError`；
        - 暂停/取消 -> `GraphRunAborted`；
        - **控制版本变化 -> `StaleResponseError`**（用户在本轮模型调用期间改变方向）。

        `pinned_control_version` 是"本模型轮次固定下来的版本"，必须由调用方传入。
        不能在这里读数据库当前值来代替 —— 那样等于拿当前值跟当前值比，
        用户改变方向就拦不住旧响应（§4.3）。
        """
        async with self._db.session() as session:
            alive = await is_current_owner(
                session, run_id=run_id, owner=owner, generation=generation
            )
            if not alive:
                raise LeaseLostError(str(run_id), owner)
            run = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
        status = RunStatus(run.status)
        if status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
            raise GraphRunAborted("cancelled", detail="派发前检查：运行已进入取消流程")
        if status in {RunStatus.PAUSING, RunStatus.PAUSED}:
            raise GraphRunAborted("paused", detail="派发前检查：运行已进入暂停流程")
        if int(generation) != int(run.owner_generation):
            raise LeaseLostError(str(run_id), owner)
        if pinned_control_version is not None and int(pinned_control_version) != int(
            run.control_version
        ):
            raise StaleResponseError(
                pinned_version=int(pinned_control_version),
                current_version=int(run.control_version),
            )

    # ------------------------------------------------------------------ #
    # 状态落库
    # ------------------------------------------------------------------ #

    async def _set_status(self, run_id: UUID, status: RunStatus, *, reason: str = "") -> None:
        async with self._db.transaction() as session:
            run = await set_run_status(session, run_id=run_id, target=status)
            await append_event(
                session,
                session_id=run.session_id,
                run_id=run_id,
                event_type=EventType.RUN_STATUS,
                payload={"status": str(status), "reason": reason},
            )

    async def _finalize(
        self,
        run_id: UUID,
        status: RunStatus,
        *,
        detail: str,
        completion_reason: CompletionReason | None,
    ) -> None:
        async with self._db.transaction() as session:
            run = (
                await session.execute(select(RunRow).where(RunRow.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                return
            await session.execute(
                select(SessionRow).where(SessionRow.id == run.session_id).with_for_update()
            )
            if status is RunStatus.PAUSED:
                if RunStatus(run.status) not in {
                    RunStatus.PAUSING,
                    RunStatus.PAUSED,
                    RunStatus.RECOVERING,
                }:
                    await set_run_status(session, run_id=run.id, target=RunStatus.PAUSING)
                from agent_workbench.contracts.enums import CommandStatus
                from agent_workbench.persistence.repositories import CommandRepository

                repo = CommandRepository(session)
                for command in await repo.list_pending(run.session_id):
                    if command.run_id == run.id and command.kind == "pause":
                        await repo.mark_status(command.id, CommandStatus.CONSUMED)
            if status is RunStatus.CANCELLED:
                if RunStatus(run.status) not in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                    if RunStatus(run.status) in {RunStatus.RECOVERING, RunStatus.PAUSING}:
                        await set_run_status(session, run_id=run.id, target=RunStatus.PAUSED)
                    await set_run_status(session, run_id=run.id, target=RunStatus.CANCELLING)
                from agent_workbench.contracts.enums import CommandStatus
                from agent_workbench.persistence.repositories import CommandRepository

                repo = CommandRepository(session)
                for command in await repo.list_pending(run.session_id):
                    if command.run_id == run.id:
                        await repo.mark_status(
                            command.id,
                            CommandStatus.CONSUMED
                            if command.kind == "cancel"
                            else CommandStatus.SUPERSEDED,
                            reason="当前运行已取消，不带入下一阶段",
                        )
            if RunStatus(run.status) is status:
                await release_run(session, run_id=run_id, owner=self._owner)
            else:
                await set_run_status(
                    session,
                    run_id=run_id,
                    target=status,
                    completion_reason=completion_reason,
                    stop_reason=detail or None,
                )
            await append_event(
                session,
                session_id=run.session_id,
                run_id=run_id,
                event_type=EventType.RUN_STATUS,
                payload={
                    "status": str(status),
                    "completion_reason": str(completion_reason) if completion_reason else None,
                    "detail": detail,
                    "note": (
                        "completed 表示本阶段已正常结束，**不意味着**网站安全或测试穷尽；"
                        "failed 表示基础设施/实现故障，**不能**当成阴性安全结论。"
                    ),
                },
            )

    async def _fail(self, run_id: UUID, code: ErrorCode, message: str) -> None:
        async with self._db.transaction() as session:
            run = (
                await session.execute(select(RunRow).where(RunRow.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                return
            await append_event(
                session,
                session_id=run.session_id,
                run_id=run_id,
                event_type=EventType.ERROR,
                payload={"code": str(code), "message": message},
            )
            with contextlib.suppress(Exception):
                await set_run_status(
                    session, run_id=run_id, target=RunStatus.FAILED, stop_reason=message
                )


def _delta_text(chunk: Any) -> str:
    """从 `stream_mode="messages"` 的块里取出可展示文本。"""
    message = chunk
    metadata = None
    if isinstance(chunk, tuple) and len(chunk) == 2:
        message, metadata = chunk
    _ = metadata
    if not isinstance(message, AIMessage):
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""
