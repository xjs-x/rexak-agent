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

from langchain_core.messages import AIMessageChunk
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import AgentRuntime, open_agent_runtime
from agent_workbench.agent.signals import (
    FinishGateRejected,
    GraphRunAborted,
    LeaseLostError,
    SegmentBoundaryReached,
)
from agent_workbench.agent.state import next_input_state
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import CompletionReason, ErrorCode, RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.domain.run_state import (
    claim_run,
    is_current_owner,
    release_run,
    set_run_status,
)
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import build_artifact_store
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


@dataclass(slots=True)
class RunOutcome:
    run_id: UUID
    status: RunStatus
    detail: str = ""
    completion_reason: CompletionReason | None = None
    segments: int = 0


@dataclass(slots=True)
class _DeltaBuffer:
    """短时间块缓冲：合并 token 增量，减少事务与推送次数。"""

    session_id: UUID
    run_id: UUID
    pending: str = ""
    last_flush: float = field(default_factory=time.monotonic)

    def add(self, text: str) -> str | None:
        self.pending += text
        now = time.monotonic()
        if (
            len(self.pending) >= DELTA_FLUSH_MAX_CHARS
            or now - self.last_flush >= DELTA_FLUSH_INTERVAL_SECONDS
        ):
            return self.take()
        return None

    def take(self) -> str | None:
        if not self.pending:
            self.last_flush = time.monotonic()
            return None
        chunk, self.pending = self.pending, ""
        self.last_flush = time.monotonic()
        return chunk


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
    ) -> None:
        self._settings = settings or get_settings()
        self._owner = owner or f"runner-{self._settings.owner_id}"
        self._db = database or get_database()
        self._on_event = on_event
        self._stopping = False

    @property
    def owner(self) -> str:
        return self._owner

    def request_stop(self) -> None:
        self._stopping = True

    # ------------------------------------------------------------------ #
    # 候选 run
    # ------------------------------------------------------------------ #

    async def _candidate_runs(
        self, *, limit: int = 5, only_run_id: UUID | None = None
    ) -> list[RunRow]:
        """找出可以推进的 run。

        - `queued`：新阶段，直接可跑；
        - `paused` / `waiting_user` / `waiting_tool`：仅当有**待消费指令**时才继续，
          否则保持等待，避免空转（§4.2）。
        """
        async with self._db.session() as session:
            queued_stmt = (
                select(RunRow)
                .where(RunRow.status == str(RunStatus.QUEUED))
                .order_by(RunRow.created_at)
                .limit(limit)
            )
            if only_run_id is not None:
                queued_stmt = select(RunRow).where(RunRow.id == only_run_id)
            queued = (await session.execute(queued_stmt)).scalars().all()
            resumable_stmt = (
                select(RunRow)
                .where(
                    RunRow.status.in_(
                        [
                            str(RunStatus.PAUSED),
                            str(RunStatus.WAITING_USER),
                            str(RunStatus.WAITING_TOOL),
                        ]
                    )
                )
                .order_by(RunRow.updated_at)
                .limit(limit)
            )
            if only_run_id is not None:
                resumable_stmt = select(RunRow).where(RunRow.id == only_run_id)
            resumable = (await session.execute(resumable_stmt)).scalars().all()

            from agent_workbench.persistence.repositories import CommandRepository

            candidates: list[RunRow] = []
            for run in list(queued):
                candidates.append(run)
            for run in resumable:
                repo = CommandRepository(session)
                if await repo.list_pending(run.session_id):
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

    async def serve(self, *, poll_interval: float = 1.0) -> None:
        """常驻循环。SSE/浏览器断开不影响它。"""
        await self._db.start()
        try:
            while not self._stopping:
                try:
                    outcome = await self.step()
                except Exception as exc:
                    log.error("runner_step_failed", error=f"{type(exc).__name__}: {exc}")
                    outcome = None
                if outcome is None:
                    await asyncio.sleep(poll_interval)
        finally:
            await self._db.close()

    # ------------------------------------------------------------------ #
    # 驱动一个 run
    # ------------------------------------------------------------------ #

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
            return await self._run_segments(run, session_row, generation)
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
        scope = ScopePolicy(ScopeSnapshot.model_validate(session_row.scope_snapshot or {}))
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
            await self._shutdown(tunnel)
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
            interactive_threshold_seconds=float(settings.tool_interactive_threshold_seconds),
            control_check=self._assert_dispatch_allowed,
        )
        context = RunContext(toolbox=toolbox, thread_id=session_row.thread_id)

        segments = 0
        status = RunStatus.RUNNING
        detail = ""
        completion_reason: CompletionReason | None = None

        try:
            runtime_ctx = open_agent_runtime(settings=settings, selection=selection)
            runtime = await runtime_ctx.__aenter__()
        except Exception as exc:
            detail = (
                f"Agent 运行时装配失败：{type(exc).__name__}: {exc}。"
                "这是配置/基础设施问题，不是目标网站的阴性结论。"
            )
            await self._fail(run.id, ErrorCode.INTERNAL_ERROR, detail)
            await self._finalize(run.id, RunStatus.FAILED, detail=detail, completion_reason=None)
            await self._shutdown(tunnel)
            return RunOutcome(run_id=run.id, status=RunStatus.FAILED, detail=detail)

        try:
            while True:
                segments += 1
                try:
                    payload = await self._segment_input(runtime, run, session_row, segments)
                    await self._stream_once(runtime, context, toolbox, session_row, run, payload)
                    status_final, reason = await self._resolve_after_segment(
                        runtime, run, session_row
                    )
                    if status_final is not None:
                        status = status_final
                        completion_reason = reason
                        break
                    if await self._segment_should_continue(run):
                        continue
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
                except Exception as exc:
                    status, detail = await self._classify_exception(run.id, exc)
                    break

        finally:
            # checkpointer / store 连接成对关闭（生命周期由本函数持有）
            with contextlib.suppress(Exception):
                await runtime_ctx.__aexit__(None, None, None)

        await self._finalize(run.id, status, detail=detail, completion_reason=completion_reason)
        await self._shutdown(tunnel)
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

    async def _shutdown(self, tunnel: SshTunnel) -> None:
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

        - 该 thread **已有** checkpoint（含本 run 之前的历史）-> 用 `None` 继续，
          框架会从 checkpoint 续跑，不会重复注入用户消息；
        - 首次输入 -> 组装最小输入（目标 + 用户原文）。
        """
        config = {"configurable": {"thread_id": session_row.thread_id}}
        try:
            snapshot = await runtime.graph.aget_state(config)
            values = (snapshot.values or {}) if snapshot is not None else {}
        except Exception as exc:
            log.warning(
                "get_state_failed",
                run_id=str(run.id),
                error=f"{type(exc).__name__}: {exc}",
            )
            values = {}

        if values.get("messages"):
            return None
        if segment_index > 1:
            # 后续片段却没有历史：说明状态读取异常，明确报错而不是重复注入目标
            raise RuntimeError(
                f"第 {segment_index} 个片段找不到线程历史，拒绝重复注入初始目标以免重复执行操作"
            )
        return next_input_state(
            session_id=run.session_id,
            run_id=run.id,
            objective=run.objective,
            user_message=run.objective,
        )

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

        async for mode, chunk in runtime.graph.astream(
            payload,
            config=config,
            context=context,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                text = _delta_text(chunk)
                if text:
                    merged = buffer.add(text)
                    if merged:
                        await self._emit_delta(run, merged)
            elif mode == "updates":
                await self._handle_update(run, toolbox, chunk)

            # 每个块之后检查一次控制指令（暂停/取消/所有权）
            if toolbox.work_units and toolbox.work_units % 5 == 0:
                await self._assert_dispatch_allowed(run.id, self._owner, toolbox.owner_generation)

        tail = buffer.take()
        if tail:
            await self._emit_delta(run, tail)

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

    async def _emit_delta(self, run: RunRow, text: str) -> None:
        await self._emit(run, EventType.MESSAGE_DELTA, {"role": "assistant", "delta": text})

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
        self, runtime: AgentRuntime, run: RunRow, session_row: SessionRow
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

        async with self._db.session() as session:
            decision = await FinishGate(session).evaluate(
                session_id=run.session_id, proposal=proposal
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
            await self._cancel_active_jobs(run)
            return RunStatus.CANCELLED, None, exc.detail or "已按用户请求取消"
        return RunStatus.PAUSED, None, exc.detail or "已按用户请求暂停"

    async def _cancel_active_jobs(self, run: RunRow) -> None:
        """取消全部活动工具并**核对结果**。

        不能在只发出取消请求时显示"已全部停止"（§4.2）。
        """
        from agent_workbench.persistence.repositories import ToolJobRepository

        async with self._db.session() as session:
            jobs = await ToolJobRepository(session).list_active(self._settings.executor_id)
        if not jobs:
            return
        tunnel = SshTunnel(self._settings)
        client = HttpExecutorClient(
            tunnel, settings=self._settings, expected_executor_id=self._settings.executor_id
        )
        try:
            await client.start()
            for job in jobs:
                import uuid as _uuid

                try:
                    receipt = await client.cancel(job.job_id, _uuid.uuid4())
                except Exception as exc:
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

    async def _classify_exception(self, run_id: UUID, exc: BaseException) -> tuple[RunStatus, str]:
        """区分"图步数上限"、"上下文溢出"和其它故障。

        **不把上限耗尽当成任务完成**（§6.4）。
        """
        name = type(exc).__name__
        message = str(exc)
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

    async def _assert_dispatch_allowed(self, run_id: UUID, owner: str, generation: int) -> None:
        """派发前检查（由工具通过 `control_check` 调用）。

        - 所有权丢失 -> `LeaseLostError`；
        - 暂停/取消 -> `GraphRunAborted`。
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
    if isinstance(message, AIMessageChunk):
        content = message.content
    else:
        content = getattr(message, "content", None)
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
