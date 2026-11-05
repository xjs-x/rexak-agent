"""持久委派意图和 A2A 任务投影。未知不重发；不共享 LangGraph 或 Executor 身份。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

import httpx
from a2a.types import Task, TaskState
from a2a.utils.errors import A2AError
from google.protobuf.json_format import MessageToDict
from pydantic import ValidationError
from sqlalchemy import or_, select, true
from sqlalchemy.dialects.postgresql import insert

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.delegation import (
    TERMINAL_DELEGATIONS,
    DelegationInput,
    DelegationRead,
    ExecutionMode,
    WorkerResult,
    WorkerTaskPackage,
)
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.run_state import is_current_owner
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient, WorkerUnavailable
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import AgentDelegationRow, ArtifactRow, RunRow, SessionRow

TRANSPORT_ERRORS = (httpx.HTTPError, A2AError, WorkerUnavailable, OSError, ValueError)
REMOTE_STATES = {
    TaskState.TASK_STATE_SUBMITTED: "accepted", TaskState.TASK_STATE_WORKING: "working",
    TaskState.TASK_STATE_COMPLETED: "completed", TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "cancelled", TaskState.TASK_STATE_REJECTED: "rejected",
}


class DelegationManager:
    def __init__(self, db: Database, settings: Settings, session_id: UUID, run_id: UUID) -> None:
        self.db, self.settings, self.session_id, self.run_id = db, settings, session_id, run_id

    async def read(self, delegation_id: UUID) -> DelegationRead:
        async with self.db.session() as db:
            row = await db.get(AgentDelegationRow, delegation_id)
            await self._authorize(db, row)
            assert row is not None
            return DelegationRead.model_validate(row)

    async def _authorize(self, db: Any, row: AgentDelegationRow | None) -> None:
        parent = await db.get(SessionRow, self.session_id)
        if (parent is None or parent.owner_id != self.settings.owner_id or parent.status == "deleted"
                or row is None or row.parent_session_id != self.session_id or row.parent_run_id != self.run_id):
            raise ApiError(ErrorCode.NOT_FOUND, "委派任务不存在")

    async def delegate(self, tool_call_id: str, value: DelegationInput, *, owner: str,
                       generation: int, control_version: int, input_delivery=None) -> DelegationRead:
        if not tool_call_id or len(tool_call_id) > 256:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "委派必须具有稳定父 ToolCall ID")
        delegation_id = uuid5(self.run_id, f"agent-delegation:{tool_call_id}")
        payload = value.model_dump(mode="json")
        if not value.attachment_ids:
            payload.pop("attachment_ids")
        if value.resume_from_delegation_id is None:
            payload.pop("resume_from_delegation_id")
        # Preserve the legacy input digest for objective/ref-only tool replays.
        if not value.background:
            payload.pop("background")
        if not value.completion_requirements:
            payload.pop("completion_requirements")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self.db.transaction() as db:
            # Lock same parent boundaries as control commands; never hold lock over network.
            parent = await db.scalar(select(SessionRow).where(SessionRow.id == self.session_id).with_for_update())
            run = await db.scalar(select(RunRow).where(RunRow.id == self.run_id).with_for_update())
            if (not parent or parent.owner_id != self.settings.owner_id or not run or run.session_id != self.session_id
                    or (run.config_snapshot or {}).get("execution_mode", "standard") != ExecutionMode.ULTRA):
                raise ApiError(ErrorCode.NOT_FOUND, "Ultra Run 不存在")
            if (run.status != "running" or run.control_version != control_version
                    or not await is_current_owner(db, run_id=self.run_id, owner=owner, generation=generation)):
                raise ApiError(ErrorCode.CONFLICT, "父 Run 控制版本或租约已过期")
            existing = await db.get(AgentDelegationRow, delegation_id)
            sources = []
            if existing is None:
                if value.attachment_ids:
                    from agent_workbench.domain.attachments import load_attachment

                    if not self.settings.ultra_worker_execution_enabled or input_delivery is None:
                        raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 附件交付未启用")
                    if len(set(value.attachment_ids)) != len(value.attachment_ids):
                        raise ApiError(ErrorCode.VALIDATION_FAILED, "附件 ID 不得重复")
                    for identifier in value.attachment_ids:
                        await load_attachment(db, self.session_id, identifier, self.settings, bound_only=True)
                if value.resume_from_delegation_id:
                    source = await db.get(AgentDelegationRow, value.resume_from_delegation_id)
                    if (not source or source.parent_session_id != self.session_id
                            or source.state != "completed" or source.cancel_requested or not source.context_id):
                        raise ApiError(ErrorCode.VALIDATION_FAILED, "续用来源必须是本 Session 已完成且未取消的 Worker")
                for ref in value.context_refs:
                    artifact = await db.get(ArtifactRow, ref)
                    if not artifact or artifact.session_id != self.session_id or artifact.availability != "available":
                        raise ApiError(ErrorCode.NOT_FOUND, "上下文工件不属于当前 Session 或不可用")
                    # Only immutable provenance. No URLs, filesystem paths, private history or credentials.
                    sources.append({"artifact_id": str(artifact.id), "sha256": artifact.sha256,
                        "byte_size": artifact.byte_size, "media_type": artifact.media_type,
                        "captured_at": artifact.captured_at.isoformat(),
                        "source_operation_id": str(artifact.source_operation_id) if artifact.source_operation_id else None})
            context_id = uuid5(delegation_id, "pi-context:v1")
            handoff = WorkerTaskPackage.model_validate({**payload, "context_id": context_id, "session_id": self.session_id, "context_sources": sources,
                "evidence_access": "session_read_v1", "collaboration_access": "collaboration_v1",
                "execution_access": "pi_tools_v1" if self.settings.ultra_worker_execution_enabled else None}).model_dump(mode="json") if existing is None else existing.handoff_snapshot
            await db.execute(insert(AgentDelegationRow).values(
                id=delegation_id, parent_session_id=self.session_id, parent_run_id=self.run_id,
                parent_tool_call_id=tool_call_id, request_digest=digest, objective=value.objective,
                context_id=context_id, handoff_snapshot=handoff,
                context_refs=payload["context_refs"], state="intent", summary="", cancel_requested=False,
                authorization_snapshot={"scope": parent.scope_snapshot, "policy": parent.policy_snapshot,
                    "identity_id": str(parent.active_identity_id) if parent.active_identity_id else None,
                    "owner_generation": generation, "control_version": control_version,
                    "tool_allowlist": (["read", "bash", "edit", "write", "grep", "find"] if self.settings.ultra_worker_execution_enabled else ["mock_observe"]) + ["list_evidence", "read_evidence", "report_finding"],
                    "executor_access": self.settings.ultra_worker_execution_enabled},
            ).on_conflict_do_nothing(index_elements=[AgentDelegationRow.id]))
            row = await db.get(AgentDelegationRow, delegation_id)
            assert row is not None
            if row.request_digest != digest:
                raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "同一父 ToolCall 的委派参数冲突")
            fresh = row.state == "intent"
            if fresh:
                row.state = "submitting"
            # Snapshot, not current artifact metadata, is the payload authority on replay.
            payload = row.handoff_snapshot or payload
        if not fresh:
            return await self.refresh(delegation_id)
        # Cancellation/pause could have committed since reservation. Stop before submission if known.
        async with self.db.session() as db:
            run = await db.get(RunRow, self.run_id)
            allowed = bool(run and run.status == "running" and run.control_version == control_version
                           and await is_current_owner(db, run_id=self.run_id, owner=owner, generation=generation))
        if not allowed:
            return await self.cancel(delegation_id)
        if payload.get("attachment_ids"):
            try:
                if input_delivery is None:
                    raise ValueError("附件交付服务不可用")
                files = await input_delivery(delegation_id, payload["attachment_ids"])
                if len(files) != len(payload["attachment_ids"]):
                    raise ValueError("附件交付回执数量不符")
                payload = WorkerTaskPackage.model_validate({**payload, "input_files": files}).model_dump(mode="json")
            except (ApiError, RuntimeError, ValueError, OSError) as exc:
                # A2A 尚未提交；失败不可伪装为 Worker 已获取文件。
                async with self.db.transaction() as db:
                    row = await db.get(AgentDelegationRow, delegation_id)
                    assert row is not None
                    row.state = "failed"
                    row.diagnostic = f"attachment_delivery_failed:{type(exc).__name__}"
                    row.summary = "附件交付失败，未启动 Worker"
                return await self.read(delegation_id)
            async with self.db.transaction() as db:
                row = await db.get(AgentDelegationRow, delegation_id)
                run = await db.get(RunRow, self.run_id)
                assert row is not None
                if (row.cancel_requested or row.state != "submitting" or not run or run.status != "running"
                        or run.control_version != control_version
                        or not await is_current_owner(db, run_id=self.run_id, owner=owner, generation=generation)):
                    row.state = "cancelled"
                    row.summary = "附件交付后父任务已暂停或取消，未启动 Worker"
                    return DelegationRead.model_validate(row)
                row.handoff_snapshot = payload
        try:
            async with A2AWorkerClient(self.settings) as client:
                task = await client.submit(delegation_id, payload)
            return await self._project(delegation_id, task)
        except TRANSPORT_ERRORS as exc:
            await self._unknown(delegation_id, f"submission_uncertain:{type(exc).__name__}")
            return await self.read(delegation_id)

    async def _unknown(self, delegation_id: UUID, diagnostic: str) -> None:
        async with self.db.transaction() as db:
            row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update())
            await self._authorize(db, row)
            assert row is not None
            if row.state not in TERMINAL_DELEGATIONS:
                row.state = "cancel_requested" if row.cancel_requested else "unknown"
                row.diagnostic = diagnostic[:256]
            row.updated_at = datetime.now(UTC)

    async def refresh(self, delegation_id: UUID) -> DelegationRead:
        record = await self.read(delegation_id)
        if record.state in TERMINAL_DELEGATIONS:
            return record
        try:
            async with A2AWorkerClient(self.settings) as client:
                if not record.a2a_task_id:
                    found = await client.lookup(delegation_id)
                    if not found:
                        await self._unknown(delegation_id, "request_not_observed_no_automatic_resubmit")
                        return await self.read(delegation_id)
                    if found.get("phase") == "cancelled" and not found.get("task"):
                        return await self._cancelled_tombstone(delegation_id)
                    task_id = str(found["task_id"])
                else:
                    task_id = record.a2a_task_id
                task = await client.get(task_id)
            return await self._project(delegation_id, task)
        except TRANSPORT_ERRORS as exc:
            await self._unknown(delegation_id, f"query_uncertain:{type(exc).__name__}")
            return await self.read(delegation_id)

    async def _project(self, delegation_id: UUID, task: Task) -> DelegationRead:
        metadata = MessageToDict(task.metadata)
        if metadata.get("request_id") != str(delegation_id) or not metadata.get("worker_id"):
            await self._unknown(delegation_id, "untrusted_task_identity")
            return await self.read(delegation_id)
        state = REMOTE_STATES.get(task.status.state, "unknown")
        if state in TERMINAL_DELEGATIONS or metadata.get("diagnostic") in {"worker_restart_no_pi_resume", "executor_operation_unresolved"}:
            from agent_workbench.domain.worker_tools import close_worker_aggregates

            async with self.db.session() as db:
                interrupted = await db.get(AgentDelegationRow, delegation_id)
                await self._authorize(db, interrupted)
            if interrupted:
                await close_worker_aggregates(self.db, interrupted)
        result: WorkerResult | None = None
        diagnostic = str(metadata.get("diagnostic", metadata.get("protocol_diagnostic", "")))[:256] or None
        if state in {"completed", "failed", "cancelled"} and (state == "completed" or task.artifacts):
            try:
                if len(task.artifacts) != 1 or len(task.artifacts[0].parts) != 1:
                    raise ValueError("invalid_artifact_count")
                raw = MessageToDict(task.artifacts[0].parts[0].data)
                encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
                if len(encoded) > 65536:
                    raise ValueError("oversized_result")
                result = WorkerResult.model_validate(raw)
                if result.task_id != task.id or result.status != state:
                    raise ValueError("untrusted_result_provenance")
                if any(len(v) > 2000 for v in result.unresolved_questions + result.suggested_next_steps):
                    raise ValueError("oversized_result_text")
            except (ValueError, ValidationError) as exc:
                # Remote terminal is retained separately; rejected result cannot become successful evidence.
                result = None
                diagnostic = f"invalid_worker_artifact:{type(exc).__name__}"
        async with self.db.transaction() as db:
            row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update())
            await self._authorize(db, row)
            assert row is not None
            if result is not None and row.context_id is not None and (
                    result.context_id != row.context_id or task.context_id != str(row.context_id)):
                result, diagnostic = None, "invalid_worker_artifact:context_identity"
            if result is not None:
                from agent_workbench.domain.worker_evidence import validate_result_evidence
                from agent_workbench.domain.worker_tools import validate_output_refs

                if not await validate_result_evidence(db, self.settings, row, result):
                    result, diagnostic = None, "invalid_worker_artifact:evidence_provenance"
                elif not await validate_output_refs(db, self.settings, row, result):
                    result, diagnostic = None, "invalid_worker_artifact:output_provenance"
            if row.state in TERMINAL_DELEGATIONS:
                return DelegationRead.model_validate(row)
            if row.a2a_task_id and row.a2a_task_id != task.id:
                row.state, row.diagnostic = "unknown", "task_id_mapping_conflict"
            else:
                row.a2a_task_id, row.worker_id = task.id, str(metadata["worker_id"])
                row.remote_state = TaskState.Name(task.status.state)
                row.state = "cancel_requested" if row.cancel_requested and state not in TERMINAL_DELEGATIONS else state
                row.diagnostic = diagnostic
                if result is not None:
                    stored = ArtifactStore(self.settings).write_bytes(
                        result.model_dump_json().encode("utf-8"), media_type="application/json",
                        captured_by=f"pi-worker:{row.worker_id}", source_operation=f"delegation:{row.id}",
                    )
                    artifact = await ArtifactRepository(db).create(
                        artifact_id=uuid5(row.id, "result"), sha256=stored.sha256,
                        byte_size=stored.byte_size, media_type=stored.media_type,
                        relative_key=stored.relative_key, session_id=self.session_id,
                        source_operation_id=None, sensitivity="internal", availability="available",
                        captured_at=stored.captured_at, captured_by_identity=f"pi-worker:{row.worker_id}",
                        summary="低信任 Pi Worker 结果；不是独立验证",
                    )
                    row.result_artifact_id, row.summary = artifact.id, result.summary
                elif diagnostic and diagnostic.startswith("invalid_worker_artifact:"):
                    row.summary = "远端执行已结束；结果校验失败，拒绝采信，未写入结果工件"
                elif state in {"failed", "cancelled", "rejected"}:
                    row.summary = f"Worker {state}; no independent verification"
            row.updated_at = datetime.now(UTC)
        return await self.read(delegation_id)

    async def _cancelled_tombstone(self, delegation_id: UUID) -> DelegationRead:
        async with self.db.transaction() as db:
            row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update())
            await self._authorize(db, row)
            assert row is not None
            if row.state not in TERMINAL_DELEGATIONS:
                row.state, row.summary = "cancelled", "服务端取消墓碑已持久化；没有创建 Worker"
        return await self.read(delegation_id)

    async def cancel(self, delegation_id: UUID) -> DelegationRead:
        record = await self.read(delegation_id)
        if record.state in TERMINAL_DELEGATIONS:
            return record
        async with self.db.transaction() as db:
            row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update())
            await self._authorize(db, row)
            assert row is not None
            if row.state in TERMINAL_DELEGATIONS:
                return DelegationRead.model_validate(row)
            row.cancel_requested, row.state = True, "cancel_requested"
        try:
            execution_confirmed = True
            if record.context_id:
                async with self.db.session() as db:
                    row = await db.get(AgentDelegationRow, delegation_id)
                if row and row.authorization_snapshot.get("executor_access"):
                    from agent_workbench.domain.worker_tools import open_executor, settle_worker
                    from agent_workbench.persistence.tables import ToolOperationRow

                    try:
                        async with self.db.session() as db:
                            has_operations = await db.scalar(select(ToolOperationRow.operation_id).where(
                                ToolOperationRow.run_id == self.run_id,
                                ToolOperationRow.execution_context["agent_delegation_id"].astext == str(row.id),
                                ToolOperationRow.execution_context["parent_worker_operation"].astext.is_not(None)).limit(1))
                        if has_operations:
                            async with open_executor(self.settings) as executor:
                                execution_confirmed = await settle_worker(self.db, self.settings, row, executor, cancel=True)
                    except (OSError, RuntimeError) as exc:
                        execution_confirmed = False
                        await self._unknown(delegation_id, f"executor_cancel_unconfirmed:{type(exc).__name__}")
            async with A2AWorkerClient(self.settings) as client:
                task = await client.cancel(delegation_id, record.a2a_task_id)
            if not execution_confirmed:
                await self._unknown(delegation_id, "executor_cancel_unconfirmed")
                return await self.read(delegation_id)
            if task is None:
                return await self._cancelled_tombstone(delegation_id)
            return await self._project(delegation_id, task)
        except TRANSPORT_ERRORS as exc:
            await self._unknown(delegation_id, f"cancel_unconfirmed:{type(exc).__name__}")
            return await self.read(delegation_id)

    async def wait(self, delegation_id: UUID, seconds: float, *, findings_after: int | None = None) -> DelegationRead:
        deadline = asyncio.get_running_loop().time() + min(max(seconds, 0), 20)
        while True:
            result = await self.refresh(delegation_id)
            if findings_after is not None:
                from agent_workbench.domain.collaboration import CollaborationService

                if await CollaborationService(self.db, self.settings).findings(self.session_id, self.run_id, findings_after):
                    return result
            if result.state in TERMINAL_DELEGATIONS or asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(0.25)

    async def cancel_all(self) -> bool:
        async with self.db.session() as db:
            ids = list((await db.scalars(select(AgentDelegationRow.id).where(
                AgentDelegationRow.parent_run_id == self.run_id,
                AgentDelegationRow.state.not_in(TERMINAL_DELEGATIONS),
            ))).all())
        semaphore = asyncio.Semaphore(4)

        async def cancel_one(delegation_id: UUID) -> bool:
            async with semaphore:
                record = await self.cancel(delegation_id)
                return record.state in TERMINAL_DELEGATIONS

        return all(await asyncio.gather(*(cancel_one(delegation_id) for delegation_id in ids)))


async def reconcile_inactive_parents(db: Database, settings: Settings, *, only_run_id: UUID | None = None) -> None:
    """独立后台轮询；取消意图跨暂停/恢复保留，不阻塞 Standard 调度。"""
    async with db.session() as session:
        rows = (await session.execute(select(
            AgentDelegationRow.id, AgentDelegationRow.parent_session_id,
            AgentDelegationRow.parent_run_id, RunRow.status, AgentDelegationRow.cancel_requested)
            .join(RunRow, AgentDelegationRow.parent_run_id == RunRow.id)
            .join(SessionRow, AgentDelegationRow.parent_session_id == SessionRow.id)
            .where(SessionRow.owner_id == settings.owner_id,
                RunRow.id == only_run_id if only_run_id else true(),
                AgentDelegationRow.state.not_in(TERMINAL_DELEGATIONS))
            .order_by(
                # Stop intents take precedence; time advances even for unchanged unknowns.
                or_(AgentDelegationRow.cancel_requested,
                    RunRow.status.in_(["pausing", "paused", "cancelling", "cancelled", "completed", "failed"])).desc(),
                AgentDelegationRow.updated_at, AgentDelegationRow.id,
            ).limit(64))).all()
    semaphore = asyncio.Semaphore(4)

    async def reconcile(row: Any) -> None:
        delegation_id, session_id, run_id, status, cancel_requested = row
        manager = DelegationManager(db, settings, session_id, run_id)
        async with semaphore:
            if cancel_requested or status in {"pausing", "paused", "cancelling", "cancelled", "completed", "failed"}:
                await manager.cancel(delegation_id)
            else:
                await manager.refresh(delegation_id)

    await asyncio.gather(*(reconcile(row) for row in rows))
