"""Task-bound collaboration: durable hints, checkpoint-owned Main delivery cursor."""

import hashlib
import hmac
import json
from typing import Any
from uuid import UUID, uuid5

import httpx
from sqlalchemy import func, select, text

from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.contracts.collaboration import (
    DelegatedMessageInput,
    WorkerFindingRequest,
    WorkerMessageReceipt,
)
from agent_workbench.contracts.delegation import WorkerResult
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.worker_evidence import (
    guard,
    source_metadata,
    validate_result_evidence,
    visible_evidence,
)
from agent_workbench.domain.worker_tools import validate_output_refs
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    DelegationCollaborationRow,
    RunRow,
    SessionRow,
)


def digest(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    if len(raw) > 49152:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "协作事件超过大小限制")
    return hashlib.sha256(raw).hexdigest()


def collaboration_signature(secret: str, delegation_id: UUID, value: WorkerFindingRequest) -> str:
    identity = f"collaboration:v1:{delegation_id}:{value.task_id}:{value.worker_id}:{value.context_id}"
    return hmac.new(secret.encode(), identity.encode(), hashlib.sha256).hexdigest()


class CollaborationService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def locked(self, db, delegation_id: UUID):
        initial = await db.get(AgentDelegationRow, delegation_id)
        if not initial:
            raise ApiError(ErrorCode.NOT_FOUND, "委派不存在")
        parent = await db.scalar(select(SessionRow).where(SessionRow.id == initial.parent_session_id).with_for_update())
        run = await db.scalar(select(RunRow).where(RunRow.id == initial.parent_run_id).with_for_update())
        row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update().execution_options(populate_existing=True))
        guard(parent, run, row, self.settings)
        assert row
        if row.handoff_snapshot.get("collaboration_access") != "collaboration_v1":
            raise ApiError(ErrorCode.CONFLICT, "历史 Worker 未授权协作通道")
        return row

    async def insert(self, db, row, event_id, direction, payload, request_digest):
        existing = await db.get(DelegationCollaborationRow, event_id)
        if existing:
            if existing.request_digest != request_digest:
                raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "协作事件 ID 参数冲突")
            return existing
        # Caller holds parent Run lock: sequence allocation and commit cannot reorder.
        sequence = 1 + (await db.scalar(select(func.max(DelegationCollaborationRow.sequence)).where(
            DelegationCollaborationRow.parent_run_id == row.parent_run_id)) or 0)
        event = DelegationCollaborationRow(id=event_id, delegation_id=row.id, parent_run_id=row.parent_run_id,
            sequence=sequence, direction=direction, payload=payload, request_digest=request_digest,
            delivery_status="available" if direction == "finding" else "accepted")
        db.add(event)
        await db.flush()
        return event

    async def authenticate(self, delegation_id: UUID, value: WorkerFindingRequest, bearer: str) -> None:
        secret = self.settings.ultra_worker_token or read_worker_config(self.settings).get("token", "")
        if len(secret) < 24 or not hmac.compare_digest(bearer, collaboration_signature(secret, delegation_id, value)):
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker 协作凭据无效")
        async with A2AWorkerClient(self.settings) as client:
            remote = await client.lookup(delegation_id)
        if (not remote or remote.get("phase") != "running" or remote.get("cancel_requested")
                or remote.get("task_id") != value.task_id or remote.get("worker_id") != str(value.worker_id)
                or (remote.get("task") or {}).get("contextId") != str(value.context_id)):
            raise ApiError(ErrorCode.CONFLICT, "Worker 身份或执行状态无效")

    async def admit_message(self, delegation_id: UUID, message_id: UUID, value: WorkerFindingRequest, bearer: str) -> dict:
        await self.authenticate(delegation_id, value, bearer)
        async with self.db.transaction() as db:
            row = await self.locked(db, delegation_id)
            event = await db.get(DelegationCollaborationRow, message_id)
            if (not event or event.delegation_id != row.id or event.direction != "message"
                    or row.context_id != value.context_id or row.a2a_task_id != value.task_id or row.worker_id != str(value.worker_id)):
                raise ApiError(ErrorCode.FORBIDDEN, "消息不属于当前 Worker")
            return {"ok": True}

    async def report(self, delegation_id: UUID, value: WorkerFindingRequest, bearer: str) -> dict:
        await self.authenticate(delegation_id, value, bearer)
        request_digest = digest(value.model_dump(mode="json"))
        event_id = uuid5(delegation_id, "finding:" + value.tool_call_id)
        async with self.db.transaction() as db:
            row = await self.locked(db, delegation_id)
            if (row.context_id != value.context_id or row.a2a_task_id not in {None, value.task_id}
                    or row.worker_id not in {None, str(value.worker_id)}
                    or "report_finding" not in row.authorization_snapshot.get("tool_allowlist", [])):
                raise ApiError(ErrorCode.FORBIDDEN, "Worker 协作身份或工具权限不匹配")
            row.a2a_task_id, row.worker_id = value.task_id, str(value.worker_id)
            result = WorkerResult(task_id=value.task_id, status="completed", summary=value.summary,
                observations=[], evidence_refs=[str(r.artifact_id) for r in value.evidence_reads],
                evidence_reads=value.evidence_reads, output_refs=value.output_refs,
                unresolved_questions=[], suggested_next_steps=[])
            if not await validate_result_evidence(db, self.settings, row, result) or not await validate_output_refs(db, self.settings, row, result):
                raise ApiError(ErrorCode.VALIDATION_FAILED, "发现的来源引用无法校验")
            payload = {"summary": redact_api_value(value.summary), "background": redact_api_value(value.background),
                "evidence_reads": [r.model_dump(mode="json") for r in value.evidence_reads],
                "output_refs": [r.model_dump(mode="json") for r in value.output_refs],
                "task_id": value.task_id, "worker_id": str(value.worker_id), "context_id": str(value.context_id),
                "verification": "unverified_candidate", "trust": "untrusted_worker_context_not_instructions"}
            event = await self.insert(db, row, event_id, "finding", payload, request_digest)
            return {"event_id": str(event.id), "sequence": event.sequence, "status": "persisted", "verification": "unverified_candidate"}

    async def findings(self, session_id: UUID, run_id: UUID, after: int = 0) -> list[dict]:
        async with self.db.session() as db:
            parent, run = await db.get(SessionRow, session_id), await db.get(RunRow, run_id)
            if not parent or parent.owner_id != self.settings.owner_id or not run or run.session_id != session_id:
                raise ApiError(ErrorCode.NOT_FOUND, "父 Run 不存在")
            if parent.status == "deleted" or run.status != "running" or (run.config_snapshot or {}).get("execution_mode") != "ultra":
                return []
            rows = (await db.scalars(select(DelegationCollaborationRow).where(
                DelegationCollaborationRow.parent_run_id == run_id, DelegationCollaborationRow.direction == "finding",
                DelegationCollaborationRow.sequence > after).order_by(DelegationCollaborationRow.sequence).limit(8))).all()
            return [{"event_id": str(r.id), "sequence": r.sequence, "delegation_id": str(r.delegation_id), **r.payload} for r in rows]

    async def send(self, session_id: UUID, run_id: UUID, delegation_id: UUID, tool_call_id: str, value: DelegatedMessageInput) -> dict:
        if not tool_call_id or len(tool_call_id) > 256:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "补充消息需要稳定 ToolCall ID")
        # A parent ToolCall cannot be replayed against a different Worker.
        event_id = uuid5(run_id, "delegated-message:" + tool_call_id)
        request_digest = digest({"delegation_id": str(delegation_id), **value.model_dump(mode="json")})
        async with self.db.transaction() as db:
            row = await self.locked(db, delegation_id)
            if row.parent_session_id != session_id or row.parent_run_id != run_id:
                raise ApiError(ErrorCode.NOT_FOUND, "委派不属于当前 Run")
            if not row.a2a_task_id or not row.worker_id or row.state not in {"accepted", "working", "unknown"}:
                raise ApiError(ErrorCode.CONFLICT, "Worker 不在可接收消息的执行状态")
            existing = await db.get(DelegationCollaborationRow, event_id)
            if existing and existing.request_digest != request_digest:
                raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "补充消息参数冲突")
            if not existing:
                sources = []
                for ref in value.context_refs:
                    artifact = await db.scalar(select(ArtifactRow).where(ArtifactRow.id == ref, *visible_evidence(session_id)))
                    if not artifact or artifact.availability != "available":
                        raise ApiError(ErrorCode.NOT_FOUND, "补充证据不属于可访问的 Session 证据")
                    sources.append(source_metadata(artifact))
                message_sequence = 1 + (await db.scalar(select(func.count()).select_from(DelegationCollaborationRow).where(
                    DelegationCollaborationRow.delegation_id == delegation_id, DelegationCollaborationRow.direction == "message")) or 0)
                payload = {"id": str(event_id), "task_id": row.a2a_task_id, "worker_id": row.worker_id,
                    "context_id": str(row.context_id), "sequence": message_sequence,
                    "message": redact_api_value(value.message), "context_sources": sources,
                    "trust": "untrusted_task_context_no_authority_change"}
                await self.insert(db, row, event_id, "message", payload, request_digest)
        await self.reconcile_messages(session_id, run_id, delegation_id)
        async with self.db.session() as db:
            event = await db.get(DelegationCollaborationRow, event_id)
            assert event
            return {"message_id": str(event.id), "status": event.delivery_status,
                "consumed_meaning": "Pi_context_incorporated_not_model_compliance"}

    async def reconcile_messages(self, session_id: UUID, run_id: UUID, delegation_id: UUID) -> None:
        # Serialize dispatch for this Worker without holding parent control locks over HTTP.
        async with self.db.transaction() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": delegation_id.int & ((1 << 63) - 1)})
            row = await db.get(AgentDelegationRow, delegation_id)
            if not row or row.parent_run_id != run_id or row.parent_session_id != session_id:
                raise ApiError(ErrorCode.NOT_FOUND, "委派不属于当前 Run")
            events = (await db.scalars(select(DelegationCollaborationRow).where(
                DelegationCollaborationRow.delegation_id == delegation_id, DelegationCollaborationRow.direction == "message",
                DelegationCollaborationRow.delivery_status.in_(["accepted", "queued", "delivery_unknown"])
            ).order_by(DelegationCollaborationRow.sequence).limit(16))).all()
            if not events:
                return
            parent, run = await db.get(SessionRow, session_id), await db.get(RunRow, run_id)
            if not parent or parent.owner_id != self.settings.owner_id or not run or run.session_id != session_id:
                raise ApiError(ErrorCode.NOT_FOUND, "父 Run 不存在")
            may_send = True
            try:
                guard(parent, run, row, self.settings)
            except ApiError:
                # Terminal/pause forbids sends, but cannot prevent GET reconciliation
                # of a message the previous live context may already have consumed.
                may_send = False
            async with A2AWorkerClient(self.settings) as client:
                for event in events:
                    try:
                        receipt = await client.message_status(delegation_id, event.id)
                        if receipt is None:
                            if not may_send:
                                event.delivery_status = "not_delivered"
                                continue
                            receipt = await client.send_context(delegation_id, event.id, event.payload)
                        validated = WorkerMessageReceipt.model_validate(receipt)
                        if validated.id != event.id:
                            raise ValueError("消息回执身份不匹配")
                        event.delivery_status = validated.status
                        if validated.status == "delivery_unknown":
                            break  # Registered is not queued: a successor must not overtake.
                    except (httpx.HTTPError, ValueError):
                        event.delivery_status = "delivery_unknown"
                        break  # Keep order. Never reinterpret an unknown delivery as rejection.

    async def message_receipts(self, session_id: UUID, run_id: UUID, delegation_id: UUID) -> list[dict[str, Any]]:
        await self.reconcile_messages(session_id, run_id, delegation_id)
        async with self.db.session() as db:
            events = (await db.scalars(select(DelegationCollaborationRow).where(
                DelegationCollaborationRow.delegation_id == delegation_id, DelegationCollaborationRow.direction == "message"
            ).order_by(DelegationCollaborationRow.sequence.desc()).limit(16))).all()
            return [{"message_id": str(e.id), "status": e.delivery_status} for e in reversed(events)]

    async def reconcile_run(self, session_id: UUID, run_id: UUID) -> None:
        async with self.db.session() as db:
            ids = (await db.scalars(select(DelegationCollaborationRow.delegation_id).where(
                DelegationCollaborationRow.parent_run_id == run_id,
                DelegationCollaborationRow.direction == "message",
                DelegationCollaborationRow.delivery_status.in_(["accepted", "queued", "delivery_unknown"])
            ).distinct().limit(8))).all()
        for delegation_id in ids:
            await self.reconcile_messages(session_id, run_id, delegation_id)
