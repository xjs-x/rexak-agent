"""仓储层：业务表的读写入口。

约定：
- 所有方法都在**调用方提供的事务**内执行，不自行提交；
- 高频查询走明确索引（见 `tables.py`）；
- 幂等键语义：**同键同内容返回原结果；同键不同内容返回冲突**（§12.1）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import (
    CommandKind,
    CommandStatus,
    ErrorCode,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CommandRow,
    ExecutionDelegationRow,
    ExecutionDeliveryBatchRow,
    ExecutionEventCursorRow,
    ExecutionEventReceiptRow,
    ExecutionResourceRow,
    ExecutionTemplateRow,
    ExecutionTemplateVersionRow,
    ExecutionWaitSetRow,
    ModelCallRow,
    ProjectRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)


def compute_request_digest(payload: Any) -> str:
    """内容摘要。

    使用规范 JSON（键排序、UTF-8、无多余空白），保证"同内容"在不同序列化下一致。
    """
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IdempotentResult(NamedTuple):
    row: CommandRow
    created: bool


# --------------------------------------------------------------------------- #
# 项目 / 会话
# --------------------------------------------------------------------------- #


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        owner_id: str,
        name: str,
        target_description: str,
        default_scope: dict[str, Any],
        settings_version: str = "1",
    ) -> ProjectRow:
        row = ProjectRow(
            id=uuid4(),
            owner_id=owner_id,
            name=name,
            target_description=target_description,
            default_scope=default_scope,
            settings_version=settings_version,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, project_id: UUID) -> ProjectRow | None:
        return (
            await self._s.execute(select(ProjectRow).where(ProjectRow.id == project_id))
        ).scalar_one_or_none()

    async def list_for_owner(self, owner_id: str) -> Sequence[ProjectRow]:
        return (
            (
                await self._s.execute(
                    select(ProjectRow)
                    .where(ProjectRow.owner_id == owner_id, ProjectRow.deleted_at.is_(None))
                    .order_by(ProjectRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        project_id: UUID,
        title: str,
        scope_snapshot: dict[str, Any],
        policy_snapshot: dict[str, Any],
        provider_id: str | None = None,
        model_name: str | None = None,
        thread_id: str | None = None,
    ) -> SessionRow:
        row = SessionRow(
            id=uuid4(),
            project_id=project_id,
            # 稳定映射：thread_id 由 session 推导，确保长期续用同一 thread
            thread_id=thread_id or f"session-{uuid4().hex}",
            title=title,
            scope_snapshot=scope_snapshot,
            policy_snapshot=policy_snapshot,
            provider_id=provider_id,
            model_name=model_name,
            status="active",
            event_seq=0,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, session_id: UUID) -> SessionRow | None:
        return (
            await self._s.execute(
                select(SessionRow).where(
                    SessionRow.id == session_id, SessionRow.status != "deleted"
                )
            )
        ).scalar_one_or_none()

    async def get_by_thread(self, thread_id: str) -> SessionRow | None:
        return (
            await self._s.execute(select(SessionRow).where(SessionRow.thread_id == thread_id))
        ).scalar_one_or_none()

    async def list_for_project(self, project_id: UUID) -> Sequence[SessionRow]:
        return (
            (
                await self._s.execute(
                    select(SessionRow)
                    .where(SessionRow.project_id == project_id, SessionRow.status != "deleted")
                    .order_by(SessionRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def update_scope(self, session_id: UUID, scope_snapshot: dict[str, Any]) -> SessionRow:
        row = await self.get(session_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
        row.scope_snapshot = scope_snapshot
        row.revision = int(row.revision) + 1
        await self._s.flush()
        return row


# --------------------------------------------------------------------------- #
# 指令（幂等提交）
# --------------------------------------------------------------------------- #


class CommandRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def submit(
        self,
        *,
        session_id: UUID,
        run_id: UUID | None,
        kind: CommandKind,
        content: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        control_version: int = 0,
    ) -> IdempotentResult:
        """幂等提交。

        - 同键同内容：返回原行，`created=False`；
        - 同键不同内容：409 `idempotency_key_reused`；
        - 并发同键：由 `uq_commands_session_idem` 兜底，捕获后重读原行。
        """
        digest = compute_request_digest(
            {"kind": str(kind), "content": content, "payload": payload or {}}
        )

        existing = await self._find(session_id, idempotency_key)
        if existing is not None:
            if existing.request_digest != digest:
                raise ApiError(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    "幂等键已用于不同内容；请使用新的 idempotency_key",
                    details={"idempotency_key": idempotency_key},
                )
            return IdempotentResult(existing, False)

        row = CommandRow(
            id=uuid4(),
            session_id=session_id,
            run_id=run_id,
            kind=str(kind),
            content=content,
            payload=payload or {},
            idempotency_key=idempotency_key,
            request_digest=digest,
            status=str(CommandStatus.QUEUED),
            priority=priority,
            control_version=control_version,
        )
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError:
            await self._s.rollback()
            existing = await self._find(session_id, idempotency_key)
            if existing is None:
                raise
            if existing.request_digest != digest:
                raise ApiError(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    "幂等键已用于不同内容；请使用新的 idempotency_key",
                    details={"idempotency_key": idempotency_key},
                ) from None
            return IdempotentResult(existing, False)
        return IdempotentResult(row, True)

    async def _find(self, session_id: UUID, idempotency_key: str) -> CommandRow | None:
        return (
            await self._s.execute(
                select(CommandRow).where(
                    CommandRow.session_id == session_id,
                    CommandRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def next_pending(self, session_id: UUID) -> CommandRow | None:
        """取优先级最高的待消费指令（高 priority 优先，其余按提交顺序）。"""
        return (
            await self._s.execute(
                select(CommandRow)
                .where(
                    CommandRow.session_id == session_id,
                    CommandRow.status == str(CommandStatus.QUEUED),
                )
                .order_by(CommandRow.priority.desc(), CommandRow.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

    async def list_pending(self, session_id: UUID) -> Sequence[CommandRow]:
        return (
            (
                await self._s.execute(
                    select(CommandRow)
                    .where(
                        CommandRow.session_id == session_id,
                        CommandRow.status == str(CommandStatus.QUEUED),
                    )
                    .order_by(CommandRow.priority.desc(), CommandRow.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

    async def mark_consumed(self, command_id: UUID, *, checkpoint_id: str | None = None) -> None:
        await self._s.execute(
            update(CommandRow)
            .where(CommandRow.id == command_id)
            .values(
                status=str(CommandStatus.CONSUMED),
                consumed_checkpoint_id=checkpoint_id,
                updated_at=_utcnow(),
            )
        )

    async def mark_status(
        self, command_id: UUID, status: CommandStatus, reason: str | None = None
    ) -> None:
        await self._s.execute(
            update(CommandRow)
            .where(CommandRow.id == command_id)
            .values(status=str(status), rejection_reason=reason, updated_at=_utcnow())
        )

    async def has_unconsumed_high_priority(self, session_id: UUID) -> bool:
        """finish_gate 检查项 1：没有未处理的高优先级用户指令。"""
        value = (
            await self._s.execute(
                select(func.count())
                .select_from(CommandRow)
                .where(
                    CommandRow.session_id == session_id,
                    CommandRow.status == str(CommandStatus.QUEUED),
                    CommandRow.priority > 0,
                )
            )
        ).scalar_one()
        return int(value) > 0


# --------------------------------------------------------------------------- #
# 工具操作 / 远端任务
# --------------------------------------------------------------------------- #


class ToolOperationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, operation_id: UUID) -> ToolOperationRow | None:
        return (
            await self._s.execute(
                select(ToolOperationRow).where(ToolOperationRow.operation_id == operation_id)
            )
        ).scalar_one_or_none()

    async def find_by_tool_call(self, run_id: UUID, tool_call_id: str) -> ToolOperationRow | None:
        """崩溃恢复：按 (run_id, tool_call_id) 找回原操作 ID（§8.5）。"""
        return (
            await self._s.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.run_id == run_id,
                    ToolOperationRow.tool_call_id == tool_call_id,
                )
            )
        ).scalar_one_or_none()

    async def prepare(
        self,
        *,
        operation_id: UUID,
        run_id: UUID,
        session_id: UUID,
        tool_call_id: str,
        tool_name: str,
        request_digest: str,
        effect_class: str,
        policy_revision: int,
        scope_revision: int,
        dispatch_control_version: int | None = None,
        execution_context: dict[str, Any] | None = None,
        identity_id: UUID | None = None,
    ) -> tuple[ToolOperationRow, bool]:
        """准备一条逻辑操作。

        返回 `(row, created)`。同 `operation_id` + 同 `request_digest` 视为重放；
        同 ID 不同 digest 直接冲突（§8.5）。
        """
        existing = await self.get(operation_id)
        if existing is not None:
            if existing.request_digest != request_digest:
                raise ApiError(
                    ErrorCode.OPERATION_CONFLICT,
                    "同一 operation_id 复用了不同参数；显式重测请生成新的逻辑操作 ID",
                    details={"operation_id": str(operation_id)},
                )
            return existing, False

        row = ToolOperationRow(
            operation_id=operation_id,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            request_digest=request_digest,
            effect_class=effect_class,
            policy_revision=policy_revision,
            scope_revision=scope_revision,
            dispatch_control_version=dispatch_control_version,
            execution_context=execution_context,
            identity_id=identity_id,
            status="accepted",
        )
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError:
            await self._s.rollback()
            existing = await self.find_by_tool_call(run_id, tool_call_id)
            if existing is None:
                raise
            if existing.request_digest != request_digest:
                raise ApiError(
                    ErrorCode.OPERATION_CONFLICT,
                    "同一 tool_call_id 已绑定不同参数的既有操作",
                ) from None
            return existing, False
        return row, True

    async def attach_job(self, operation_id: UUID, job_id: UUID, *, status: str) -> None:
        await self._s.execute(
            update(ToolOperationRow)
            .where(ToolOperationRow.operation_id == operation_id)
            .values(job_id=job_id, status=status, updated_at=_utcnow())
        )

    async def mark_result(
        self, operation_id: UUID, *, status: str, artifact_ids: Sequence[str] = ()
    ) -> None:
        await self._s.execute(
            update(ToolOperationRow)
            .where(ToolOperationRow.operation_id == operation_id)
            .values(
                status=status,
                result_artifact_ids=list(artifact_ids),
                updated_at=_utcnow(),
            )
        )


class ToolJobRepository:
    """控制端对 Executor 台账的**同步投影**，不是真实执行状态的权威。"""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self,
        *,
        job_id: UUID,
        operation_id: UUID,
        executor_id: str,
        state: str,
        remote_revision: int = 0,
        output_cursor: str | None = None,
        sync_status: str = "in_sync",
        cancel_requested: bool = False,
        cancel_confirmed: bool = False,
        executor_accepted_at: datetime | None = None,
        executor_started_at: datetime | None = None,
        executor_finished_at: datetime | None = None,
    ) -> ToolJobRow:
        row = (
            await self._s.execute(select(ToolJobRow).where(ToolJobRow.job_id == job_id))
        ).scalar_one_or_none()
        if row is None:
            row = ToolJobRow(
                job_id=job_id,
                operation_id=operation_id,
                executor_id=executor_id,
                state=state,
            )
            self._s.add(row)
        row.state = state
        row.remote_revision = remote_revision
        row.output_cursor = output_cursor
        row.sync_status = sync_status
        row.last_synced_at = _utcnow()
        if row.executor_accepted_at is None and executor_accepted_at is not None:
            row.executor_accepted_at = executor_accepted_at
        if row.executor_started_at is None and executor_started_at is not None:
            row.executor_started_at = executor_started_at
        if row.executor_finished_at is None and executor_finished_at is not None:
            row.executor_finished_at = executor_finished_at
        if cancel_requested:
            row.cancel_requested_at = row.cancel_requested_at or _utcnow()
        row.cancel_confirmed = cancel_confirmed
        await self._s.flush()
        return row

    async def list_active(self, executor_id: str | None = None) -> Sequence[ToolJobRow]:
        stmt = select(ToolJobRow).where(
            ToolJobRow.state.in_(["received", "starting", "running", "unknown"])
        )
        if executor_id:
            stmt = stmt.where(ToolJobRow.executor_id == executor_id)
        return (await self._s.execute(stmt)).scalars().all()

    async def list_for_run(
        self, run_id: UUID, *, active_only: bool = False
    ) -> Sequence[ToolJobRow]:
        """只返回归属指定 run 的任务，供取消和模型工具做边界检查。"""
        stmt = (
            select(ToolJobRow)
            .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
            .where(ToolOperationRow.run_id == run_id)
        )
        if active_only:
            stmt = stmt.where(ToolJobRow.state.in_(["received", "starting", "running", "unknown"]))
        return (await self._s.execute(stmt)).scalars().all()

    async def get_for_run(self, run_id: UUID, job_id: UUID) -> ToolJobRow | None:
        stmt = (
            select(ToolJobRow)
            .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
            .where(ToolOperationRow.run_id == run_id, ToolJobRow.job_id == job_id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_session(self, session_id: UUID) -> Sequence[ToolJobRow]:
        stmt = (
            select(ToolJobRow)
            .join(
                ToolOperationRow,
                ToolOperationRow.job_id == ToolJobRow.job_id,
            )
            .where(ToolOperationRow.session_id == session_id)
            .order_by(ToolJobRow.created_at.desc())
        )
        return (await self._s.execute(stmt)).scalars().all()


# --------------------------------------------------------------------------- #
# 工件元数据 / 模型调用
# --------------------------------------------------------------------------- #


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        artifact_id: UUID | None = None,
        sha256: str,
        byte_size: int,
        media_type: str,
        relative_key: str,
        session_id: UUID | None,
        source_operation_id: UUID | None,
        sensitivity: str,
        availability: str,
        captured_at: datetime,
        truncated: bool = False,
        captured_by_identity: str | None = None,
        source_url: str | None = None,
        summary: str | None = None,
    ) -> ArtifactRow:
        row = ArtifactRow(
            id=artifact_id or uuid4(),
            sha256=sha256,
            byte_size=byte_size,
            media_type=media_type,
            relative_key=relative_key,
            session_id=session_id,
            source_operation_id=source_operation_id,
            sensitivity=sensitivity,
            availability=availability,
            captured_at=captured_at,
            truncated=truncated,
            captured_by_identity=captured_by_identity,
            source_url=source_url,
            summary=summary,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, artifact_id: UUID) -> ArtifactRow | None:
        return (
            await self._s.execute(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
        ).scalar_one_or_none()

    async def mark_availability(self, artifact_id: UUID, availability: str) -> None:
        await self._s.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id == artifact_id)
            .values(availability=availability, updated_at=_utcnow())
        )

    async def protect(self, artifact_ids: Sequence[UUID]) -> None:
        """报告与有效发现引用的工件不能被普通清理任务删除（§5.3）。"""
        if not artifact_ids:
            return
        await self._s.execute(
            update(ArtifactRow)
            .where(ArtifactRow.id.in_(list(artifact_ids)))
            .values(protected=True, updated_at=_utcnow())
        )

    async def find_missing_files(self, *, keys_present: set[str]) -> Sequence[ArtifactRow]:
        """返回元数据标记 available 但文件缺失的行。"""
        rows = (
            (
                await self._s.execute(
                    select(ArtifactRow).where(ArtifactRow.availability == "available")
                )
            )
            .scalars()
            .all()
        )
        return [row for row in rows if row.relative_key not in keys_present]


class ModelCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def begin_attempt(
        self,
        *,
        logical_call_id: UUID,
        model_call_id: UUID | None = None,
        run_id: UUID,
        session_id: UUID,
        phase: str,
        retry_index: int,
        provider_id: str,
        model_name: str,
        price_snapshot: dict[str, Any] | None = None,
        attempts_observable: bool = True,
    ) -> ModelCallRow:
        row = ModelCallRow(
            model_call_id=model_call_id or uuid4(),
            logical_call_id=logical_call_id,
            run_id=run_id,
            session_id=session_id,
            phase=phase,
            retry_index=retry_index,
            provider_id=provider_id,
            model_name=model_name,
            price_snapshot=price_snapshot or {},
            attempts_observable=attempts_observable,
            submitted_at=_utcnow(),
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, model_call_id: UUID) -> ModelCallRow | None:
        return (
            await self._s.execute(
                select(ModelCallRow).where(ModelCallRow.model_call_id == model_call_id)
            )
        ).scalar_one_or_none()

    async def reconcile(
        self,
        *,
        model_call_id: UUID,
        usage_status: str,
        cost_status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
        reasoning_tokens: int | None,
        raw_usage: dict[str, Any],
        cost_amount: Any | None,
        cost_currency: str | None,
        first_token_at: datetime | None = None,
        completed_at: datetime | None = None,
        error_code: str | None = None,
    ) -> ModelCallRow:
        """按 `model_call_id` **幂等更新结算**。

        流式累计值 + 最终总量重复写入同一次调用，不逐块累加再加总量（§11.3 规则 2）。
        """
        row = await self.get(model_call_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"model_call 不存在：{model_call_id}")
        row.usage_status = usage_status
        row.cost_status = cost_status
        row.input_tokens = input_tokens
        row.output_tokens = output_tokens
        row.cache_read_tokens = cache_read_tokens
        row.cache_write_tokens = cache_write_tokens
        row.reasoning_tokens = reasoning_tokens
        row.raw_usage = raw_usage
        row.cost_amount = cost_amount
        row.cost_currency = cost_currency
        if first_token_at is not None:
            row.first_token_at = first_token_at
        if completed_at is not None:
            row.completed_at = completed_at
        if error_code is not None:
            row.error_code = error_code
        await self._s.flush()
        return row

    async def list_pending_sync(self) -> Sequence[ModelCallRow]:
        """持久化失败时留下的待同步用量，幂等重放（§11.3 规则 6）。"""
        return (
            (
                await self._s.execute(
                    select(ModelCallRow)
                    .where(ModelCallRow.pending_sync.is_(True))
                    .order_by(ModelCallRow.created_at)
                )
            )
            .scalars()
            .all()
        )

    async def list_for_run(self, run_id: UUID) -> Sequence[ModelCallRow]:
        return (
            (
                await self._s.execute(
                    select(ModelCallRow)
                    .where(ModelCallRow.run_id == run_id)
                    .order_by(ModelCallRow.created_at)
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# 执行底座（V4）：资源投影 / wait-set / 委托 / 事件 / delivery / 模板
# --------------------------------------------------------------------------- #


class ExecutionResourceRepository:
    """会话资源在控制端的只读投影。**不**声明为真实执行状态。"""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self,
        *,
        resource_id: UUID,
        session_id: UUID,
        executor_id: str,
        kind: str,
        state: str,
        interaction_state: str = "not_applicable",
        mode: str = "",
        purpose: str = "",
        origin_run_id: UUID | None = None,
        origin_operation_id: UUID | None = None,
        exit_code: int | None = None,
        termination_reason: str | None = None,
        output_limit_action: str = "preserve_process",
        quota: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        sync_status: str = "in_sync",
    ) -> ExecutionResourceRow:
        # 同一逻辑操作的并发回执可能同时首次投影。插入与逐行更新都在当前事务内串行。
        await self._s.execute(pg_insert(ExecutionResourceRow).values(
            resource_id=resource_id, session_id=session_id, executor_id=executor_id,
            kind=kind, state=state,
        ).on_conflict_do_nothing(index_elements=[ExecutionResourceRow.resource_id]))
        row = (await self._s.execute(select(ExecutionResourceRow).where(
            ExecutionResourceRow.resource_id == resource_id
        ).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if row.session_id != session_id or row.executor_id != executor_id:
            raise ApiError(ErrorCode.CONFLICT, "执行资源投影归属冲突，拒绝覆盖")
        checked_at = _utcnow()
        row.executor_id = executor_id
        row.kind = kind
        row.state = state
        row.interaction_state = interaction_state
        if mode:
            row.mode = mode
        if purpose:
            row.purpose = purpose
        if origin_run_id is not None and row.origin_run_id is None:
            row.origin_run_id = origin_run_id
        if origin_operation_id is not None and row.origin_operation_id is None:
            row.origin_operation_id = origin_operation_id
        row.exit_code = exit_code
        row.termination_reason = termination_reason
        row.output_limit_action = output_limit_action
        if quota is not None:
            row.quota = quota
        if detail is not None:
            row.detail = {**(row.detail or {}), **detail}
        row.sync_status = sync_status
        row.last_checked_at = checked_at
        await self._s.flush()
        return row

    async def get(self, resource_id: UUID) -> ExecutionResourceRow | None:
        return (
            await self._s.execute(
                select(ExecutionResourceRow).where(
                    ExecutionResourceRow.resource_id == resource_id
                )
            )
        ).scalar_one_or_none()

    async def list_for_session(
        self,
        session_id: UUID,
        *,
        active_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ExecutionResourceRow]:
        stmt = select(ExecutionResourceRow).where(ExecutionResourceRow.session_id == session_id)
        if active_only:
            stmt = stmt.where(
                ExecutionResourceRow.state.in_(["starting", "running", "unknown"])
            )
        stmt = (
            stmt.order_by(ExecutionResourceRow.created_at.desc()).limit(limit).offset(offset)
        )
        return (await self._s.execute(stmt)).scalars().all()

    async def active_for_session(self, session_id: UUID) -> Sequence[ExecutionResourceRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionResourceRow).where(
                        ExecutionResourceRow.session_id == session_id,
                        ExecutionResourceRow.state.in_(["starting", "running", "unknown"]),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def mark_close_requested(self, resource_id: UUID) -> None:
        await self._s.execute(
            update(ExecutionResourceRow)
            .where(ExecutionResourceRow.resource_id == resource_id)
            .values(close_state="close_requested", updated_at=_utcnow())
        )

    async def apply_close_result(
        self, resource_id: UUID, *, confirmed: bool, state: str, detail: dict[str, Any] | None = None
    ) -> None:
        values: dict[str, Any] = {
            "close_confirmed": confirmed,
            "close_state": "closed" if confirmed else "close_requested",
            "state": state,
            "updated_at": _utcnow(),
            "last_checked_at": _utcnow(),
        }
        if detail is not None:
            row = await self.get(resource_id)
            merged = dict(row.detail if row is not None else {})
            merged.update(detail)
            values["detail"] = merged
        await self._s.execute(
            update(ExecutionResourceRow)
            .where(ExecutionResourceRow.resource_id == resource_id)
            .values(**values)
        )


class ExecutionWaitSetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        session_id: UUID,
        run_id: UUID | None,
        resource_ids: list[str],
        event_types: list[str],
        match: str,
        creator_operation_id: UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ExecutionWaitSetRow:
        row = ExecutionWaitSetRow(
            id=uuid4(),
            session_id=session_id,
            run_id=run_id,
            resource_ids=list(resource_ids),
            event_types=list(event_types),
            match=match,
            status="open",
            creator_operation_id=creator_operation_id,
            detail=detail or {},
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, wait_set_id: UUID) -> ExecutionWaitSetRow | None:
        return (
            await self._s.execute(
                select(ExecutionWaitSetRow).where(ExecutionWaitSetRow.id == wait_set_id)
            )
        ).scalar_one_or_none()

    async def open_for_run(self, run_id: UUID) -> Sequence[ExecutionWaitSetRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionWaitSetRow).where(
                        ExecutionWaitSetRow.run_id == run_id,
                        ExecutionWaitSetRow.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )

    async def open_for_session(self, session_id: UUID) -> Sequence[ExecutionWaitSetRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionWaitSetRow).where(
                        ExecutionWaitSetRow.session_id == session_id,
                        ExecutionWaitSetRow.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )

    async def mark_satisfied(self, wait_set_id: UUID, *, detail: dict[str, Any] | None = None) -> None:
        values: dict[str, Any] = {
            "status": "satisfied",
            "satisfied_at": _utcnow(),
            "last_checked_at": _utcnow(),
        }
        if detail is not None:
            row = await self.get(wait_set_id)
            merged = dict(row.detail if row is not None else {})
            merged.update(detail)
            values["detail"] = merged
        await self._s.execute(
            update(ExecutionWaitSetRow).where(ExecutionWaitSetRow.id == wait_set_id).values(**values)
        )

    async def cancel_for_run(self, run_id: UUID, *, reason: str) -> int:
        result = await self._s.execute(
            update(ExecutionWaitSetRow)
            .where(
                ExecutionWaitSetRow.run_id == run_id,
                ExecutionWaitSetRow.status == "open",
            )
            .values(status="cancelled", updated_at=_utcnow(), detail={"reason": reason})
        )
        return int(getattr(result, "rowcount", 0) or 0)


class ExecutionDelegationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        session_id: UUID,
        resource_ids: list[str],
        event_filters: list[str],
        goal: str,
        mode: str,
        authorization_source: str,
        authorization_quote: str,
        authorization_command_id: UUID | None,
        created_by_run_id: UUID | None,
    ) -> ExecutionDelegationRow:
        row = ExecutionDelegationRow(
            id=uuid4(),
            session_id=session_id,
            resource_ids=list(resource_ids),
            event_filters=list(event_filters),
            goal=goal,
            mode=mode,
            status="active",
            revision=1,
            authorization_source=authorization_source,
            authorization_command_id=authorization_command_id,
            authorization_quote=authorization_quote,
            created_by_run_id=created_by_run_id,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, delegation_id: UUID) -> ExecutionDelegationRow | None:
        return (
            await self._s.execute(
                select(ExecutionDelegationRow).where(ExecutionDelegationRow.id == delegation_id)
            )
        ).scalar_one_or_none()

    async def list_for_session(
        self, session_id: UUID, *, include_cancelled: bool = True
    ) -> Sequence[ExecutionDelegationRow]:
        stmt = select(ExecutionDelegationRow).where(
            ExecutionDelegationRow.session_id == session_id
        )
        if not include_cancelled:
            stmt = stmt.where(ExecutionDelegationRow.status == "active")
        return (
            (await self._s.execute(stmt.order_by(ExecutionDelegationRow.created_at.desc())))
            .scalars()
            .all()
        )

    async def active_for_session(self, session_id: UUID) -> Sequence[ExecutionDelegationRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionDelegationRow).where(
                        ExecutionDelegationRow.session_id == session_id,
                        ExecutionDelegationRow.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )

    async def cancel(self, delegation_id: UUID, *, reason: str) -> ExecutionDelegationRow | None:
        """取消委托：递增 revision + 置 cancelled。**不向资源发送 kill**。"""
        row = await self.get(delegation_id)
        if row is None:
            return None
        await self._s.execute(
            select(SessionRow).where(SessionRow.id == row.session_id).with_for_update()
        )
        await self._s.refresh(row)
        if row.status == "active":
            row.status = "cancelled"
            row.revision = int(row.revision) + 1
            row.cancelled_at = _utcnow()
            row.cancel_reason = reason
            batches = (await self._s.execute(
                select(ExecutionDeliveryBatchRow).where(
                    ExecutionDeliveryBatchRow.delegation_id == delegation_id,
                    ExecutionDeliveryBatchRow.status.in_(["prepared", "injected"]),
                )
            )).scalars().all()
            for batch in batches:
                batch.status = "cancelled"
                if batch.message_command_id is not None:
                    await self._s.execute(
                        update(CommandRow).where(
                            CommandRow.id == batch.message_command_id,
                            CommandRow.status == "queued",
                        ).values(status="superseded")
                    )
                await self._s.execute(
                    update(ExecutionEventReceiptRow).where(
                        ExecutionEventReceiptRow.consumed_by_batch == batch.id
                    ).values(consumed_by_batch=None)
                )
            await self._s.flush()
        return row

    async def complete_once(self, delegation_id: UUID) -> None:
        row = await self.get(delegation_id)
        if row is None or row.mode != "once":
            return
        row.status = "completed"
        row.revision = int(row.revision) + 1
        await self._s.flush()

    async def touch_triggered(self, delegation_id: UUID) -> None:
        row = await self.get(delegation_id)
        if row is None:
            return
        row.last_triggered_at = _utcnow()
        row.triggered_count = int(row.triggered_count) + 1
        await self._s.flush()


class ExecutionEventReceiptRepository:
    """Executor 事件的幂等接收投影。"""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_if_new(
        self,
        *,
        executor_id: str,
        event_id: str,
        session_id: UUID | None,
        resource_id: str | None,
        operation_id: str | None,
        type: str,
        occurred_at: datetime,
        sequence: int,
        availability: str,
        payload: dict[str, Any],
    ) -> tuple[ExecutionEventReceiptRow, bool]:
        existing = (
            await self._s.execute(
                select(ExecutionEventReceiptRow).where(
                    ExecutionEventReceiptRow.executor_id == executor_id,
                    ExecutionEventReceiptRow.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        row = ExecutionEventReceiptRow(
            executor_id=executor_id,
            event_id=event_id,
            session_id=session_id,
            resource_id=resource_id,
            operation_id=operation_id,
            type=type,
            occurred_at=occurred_at,
            sequence=sequence,
            availability=availability,
            payload=payload,
        )
        try:
            async with self._s.begin_nested():
                self._s.add(row)
                await self._s.flush()
        except IntegrityError:
            existing = (
                await self._s.execute(
                    select(ExecutionEventReceiptRow).where(
                        ExecutionEventReceiptRow.executor_id == executor_id,
                        ExecutionEventReceiptRow.event_id == event_id,
                    )
                )
            ).scalar_one()
            return existing, False
        return row, True

    async def list_unconsumed(
        self, session_id: UUID, *, limit: int = 200
    ) -> Sequence[ExecutionEventReceiptRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionEventReceiptRow)
                    .where(
                        ExecutionEventReceiptRow.session_id == session_id,
                        ExecutionEventReceiptRow.consumed_by_batch.is_(None),
                    )
                    .order_by(ExecutionEventReceiptRow.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def mark_consumed(self, receipt_ids: Sequence[int], batch_id: UUID) -> None:
        if not receipt_ids:
            return
        await self._s.execute(
            update(ExecutionEventReceiptRow)
            .where(ExecutionEventReceiptRow.id.in_(list(receipt_ids)))
            .values(consumed_by_batch=batch_id)
        )


class ExecutionEventCursorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, executor_id: str) -> int:
        row = (
            await self._s.execute(
                select(ExecutionEventCursorRow).where(
                    ExecutionEventCursorRow.executor_id == executor_id
                )
            )
        ).scalar_one_or_none()
        return int(row.last_seq) if row is not None else 0

    async def put(self, executor_id: str, last_seq: int) -> None:
        row = (
            await self._s.execute(
                select(ExecutionEventCursorRow).where(
                    ExecutionEventCursorRow.executor_id == executor_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            self._s.add(ExecutionEventCursorRow(executor_id=executor_id, last_seq=last_seq))
        else:
            row.last_seq = max(int(row.last_seq), int(last_seq))
        await self._s.flush()


class ExecutionDeliveryBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        session_id: UUID,
        run_id: UUID | None,
        delegation_id: UUID | None,
        event_receipt_ids: list[int],
        summary: dict[str, Any],
        message_id: str,
        batch_id: UUID | None = None,
    ) -> ExecutionDeliveryBatchRow:
        row = ExecutionDeliveryBatchRow(
            id=batch_id or uuid4(),
            session_id=session_id,
            run_id=run_id,
            delegation_id=delegation_id,
            event_receipt_ids=list(event_receipt_ids),
            summary=summary,
            status="prepared",
            message_id=message_id,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def get(self, batch_id: UUID) -> ExecutionDeliveryBatchRow | None:
        return (
            await self._s.execute(
                select(ExecutionDeliveryBatchRow).where(ExecutionDeliveryBatchRow.id == batch_id)
            )
        ).scalar_one_or_none()

    async def get_by_message_id(self, message_id: str) -> ExecutionDeliveryBatchRow | None:
        return (
            await self._s.execute(
                select(ExecutionDeliveryBatchRow).where(
                    ExecutionDeliveryBatchRow.message_id == message_id
                )
            )
        ).scalar_one_or_none()

    async def list_open_for_session(self, session_id: UUID) -> Sequence[ExecutionDeliveryBatchRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionDeliveryBatchRow).where(
                        ExecutionDeliveryBatchRow.session_id == session_id,
                        ExecutionDeliveryBatchRow.status.in_(["prepared", "injected"]),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def mark_injected(self, batch_id: UUID, *, command_id: UUID) -> None:
        await self._s.execute(
            update(ExecutionDeliveryBatchRow)
            .where(ExecutionDeliveryBatchRow.id == batch_id)
            .values(status="injected", message_command_id=command_id, updated_at=_utcnow())
        )

    async def mark_delivered(self, batch_id: UUID) -> None:
        await self._s.execute(
            update(ExecutionDeliveryBatchRow)
            .where(ExecutionDeliveryBatchRow.id == batch_id)
            .values(status="delivered", delivered_at=_utcnow(), updated_at=_utcnow())
        )


class ExecutionTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_by_name(self, session_id: UUID, name: str) -> ExecutionTemplateRow | None:
        return (
            await self._s.execute(
                select(ExecutionTemplateRow).where(
                    ExecutionTemplateRow.session_id == session_id,
                    ExecutionTemplateRow.name == name,
                )
            )
        ).scalar_one_or_none()

    async def create_template(
        self, *, session_id: UUID, name: str, description: str, kind: str
    ) -> ExecutionTemplateRow:
        row = ExecutionTemplateRow(
            id=uuid4(),
            session_id=session_id,
            name=name,
            description=description,
            kind=kind,
            latest_revision=0,
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def add_version(
        self,
        *,
        template_id: UUID,
        spec: dict[str, Any],
        content_hash: str,
        script_artifact_id: UUID | None,
        created_by_run_id: UUID | None,
    ) -> ExecutionTemplateVersionRow:
        template = (
            await self._s.execute(
                select(ExecutionTemplateRow).where(ExecutionTemplateRow.id == template_id).with_for_update()
            )
        ).scalar_one()
        revision = int(template.latest_revision) + 1
        row = ExecutionTemplateVersionRow(
            id=uuid4(),
            template_id=template_id,
            revision=revision,
            content_hash=content_hash,
            spec=spec,
            script_artifact_id=script_artifact_id,
            created_by_run_id=created_by_run_id,
        )
        self._s.add(row)
        template.latest_revision = revision
        await self._s.flush()
        return row

    async def get_version(
        self, template_id: UUID, revision: int | None
    ) -> ExecutionTemplateVersionRow | None:
        stmt = select(ExecutionTemplateVersionRow).where(
            ExecutionTemplateVersionRow.template_id == template_id
        )
        if revision is None:
            stmt = stmt.order_by(ExecutionTemplateVersionRow.revision.desc()).limit(1)
        else:
            stmt = stmt.where(ExecutionTemplateVersionRow.revision == revision)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_session(self, session_id: UUID) -> Sequence[ExecutionTemplateRow]:
        return (
            (
                await self._s.execute(
                    select(ExecutionTemplateRow)
                    .where(ExecutionTemplateRow.session_id == session_id)
                    .order_by(ExecutionTemplateRow.name)
                )
            )
            .scalars()
            .all()
        )


def default_owner_id() -> str:
    return get_settings().owner_id
