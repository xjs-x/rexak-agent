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


def default_owner_id() -> str:
    return get_settings().owner_id
