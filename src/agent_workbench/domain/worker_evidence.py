"""Task-bound, read-only evidence delivery. No Worker business DB or filesystem access."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import exists, select

from agent_workbench.config.settings import Settings
from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.contracts.delegation import WorkerResult
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.worker_evidence import WorkerEvidenceRequest
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    DelegationEvidenceReadRow,
    ModelCallRow,
    RunRow,
    SessionRow,
    ToolOperationRow,
)

MAX_SOURCE_BYTES = 8 * 1024 * 1024


def evidence_signature(secret: str, delegation_id: UUID, value: WorkerEvidenceRequest) -> str:
    identity = f"evidence:v1:{delegation_id}:{value.task_id}:{value.worker_id}:{value.context_id}"
    return hmac.new(secret.encode(), identity.encode(), hashlib.sha256).hexdigest()


def visible_evidence(session_id: UUID):
    # Model archives and internal delivery receipts are not target evidence/private-history inputs.
    return (ArtifactRow.session_id == session_id, ArtifactRow.sensitivity != "secret",
        ~ArtifactRow.captured_by_identity.like("worker-transport:%") | ArtifactRow.captured_by_identity.is_(None),
        # Also classify Stage 3 artifacts written before transport tagging was added.
        ~exists(select(ToolOperationRow.operation_id).where(
            ToolOperationRow.operation_id == ArtifactRow.source_operation_id,
            ToolOperationRow.execution_context["parent_worker_operation"].astext.is_not(None),
            ToolOperationRow.tool_name.like("pi.%"), ~ToolOperationRow.tool_name.like("pi.bash.%"))),
        ~ArtifactRow.captured_by_identity.like("worker-evidence:%") | ArtifactRow.captured_by_identity.is_(None),
        ~exists(select(ModelCallRow.model_call_id).where(ModelCallRow.response_artifact_id == ArtifactRow.id)))


def source_metadata(row: ArtifactRow) -> dict[str, Any]:
    return {"artifact_id": str(row.id), "sha256": row.sha256, "byte_size": row.byte_size,
        "media_type": row.media_type, "availability": row.availability,
        "source_operation_id": str(row.source_operation_id) if row.source_operation_id else None,
        "captured_at": row.captured_at.isoformat(), "source_truncated": row.truncated,
        "summary": str(redact_api_value(row.summary or ""))[:256]}


def verified_source(store: ArtifactStore, row: ArtifactRow) -> bytes:
    if row.byte_size > MAX_SOURCE_BYTES:
        raise ValueError("source_too_large_use_existing_smaller_artifact")
    raw = store.read_bytes(row.relative_key, limit=MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES or len(raw) != row.byte_size or hashlib.sha256(raw).hexdigest() != row.sha256:
        raise ValueError("source_integrity_mismatch")
    return raw


def read_view(store: ArtifactStore, row: ArtifactRow, offset: int, limit: int) -> dict[str, Any]:
    """Hash the exact bytes rendered, then redact the complete bounded source before paging."""
    mime = row.media_type.partition(";")[0].strip().lower()
    if not (mime.startswith("text/") or mime.endswith(("+json", "+xml")) or mime in {"application/json", "application/xml", "application/javascript"}):
        raise ValueError("unsupported_media_type")
    raw = verified_source(store, row)
    text = raw.decode("utf-8")  # No guessing or replacement decoding of external evidence.
    view = str(redact_api_value(text)).encode("utf-8")
    if offset > len(view) or (offset < len(view) and view[offset] & 0xC0 == 0x80):
        raise ValueError("invalid_view_byte_offset")
    end = min(len(view), offset + limit)
    while end > offset and end < len(view) and view[end] & 0xC0 == 0x80:
        end -= 1
    return {**source_metadata(row), "view": "redacted_utf8_v1", "view_sha256": hashlib.sha256(view).hexdigest(),
        "offset": offset, "returned_bytes": end - offset, "next_offset": end,
        "view_total_bytes": len(view), "truncated": end < len(view), "text": view[offset:end].decode("utf-8")}


def guard(parent: SessionRow | None, run: RunRow | None, row: AgentDelegationRow | None, settings: Settings) -> None:
    if (not parent or parent.owner_id != settings.owner_id or parent.status == "deleted" or not run or not row
            or row.parent_session_id != parent.id or row.parent_run_id != run.id or run.session_id != parent.id):
        raise ApiError(ErrorCode.NOT_FOUND, "委派证据任务不存在")
    auth = row.authorization_snapshot
    if (run.status != "running" or row.cancel_requested or auth.get("execution_revoked") or row.state in {"completed", "failed", "cancelled", "rejected"}
            or (run.config_snapshot or {}).get("execution_mode") != "ultra"
            or row.handoff_snapshot.get("evidence_access") != "session_read_v1"
            or auth.get("owner_generation") != run.owner_generation or auth.get("control_version") != run.control_version
            or not run.owner or not run.lease_until or run.lease_until <= datetime.now(UTC)):
        raise ApiError(ErrorCode.CONFLICT, "委派已停止、控制版本或租约失效")


class WorkerEvidenceService:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    async def handle(self, delegation_id: UUID, value: WorkerEvidenceRequest, bearer: str) -> dict[str, Any]:
        secret = self.settings.ultra_worker_token or read_worker_config(self.settings).get("token", "")
        if len(secret) < 24 or not hmac.compare_digest(bearer, evidence_signature(secret, delegation_id, value)):
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker 证据凭据无效")
        # Validate remote durable identity even when first callback races Python submit acknowledgement.
        async with A2AWorkerClient(self.settings) as client:
            remote = await client.lookup(delegation_id)
        if (not remote or remote.get("phase") != "running" or remote.get("cancel_requested")
                or remote.get("task_id") != value.task_id or remote.get("worker_id") != str(value.worker_id)
                or (remote.get("task") or {}).get("contextId") != str(value.context_id)):
            raise ApiError(ErrorCode.CONFLICT, "远端委派身份或执行状态不允许读取")
        digest = hashlib.sha256(value.model_dump_json().encode()).hexdigest()
        receipt_id = uuid5(delegation_id, "evidence:" + value.tool_call_id)
        store = ArtifactStore(self.settings)
        async with self.db.transaction() as db:
            initial = await db.get(AgentDelegationRow, delegation_id)
            if not initial:
                raise ApiError(ErrorCode.NOT_FOUND, "委派证据任务不存在")
            parent = await db.scalar(select(SessionRow).where(SessionRow.id == initial.parent_session_id).with_for_update())
            run = await db.scalar(select(RunRow).where(RunRow.id == initial.parent_run_id).with_for_update())
            row = await db.scalar(select(AgentDelegationRow).where(AgentDelegationRow.id == delegation_id).with_for_update().execution_options(populate_existing=True))
            guard(parent, run, row, self.settings)
            assert row and parent
            if ("list_evidence" if value.action == "list" else "read_evidence") not in row.authorization_snapshot.get("tool_allowlist", []):
                raise ApiError(ErrorCode.FORBIDDEN, "委派授权快照不允许该证据工具")
            if (row.context_id != value.context_id or (row.a2a_task_id and row.a2a_task_id != value.task_id)
                    or (row.worker_id and row.worker_id != str(value.worker_id))):
                raise ApiError(ErrorCode.CONFLICT, "本地委派身份不匹配")
            row.a2a_task_id, row.worker_id = value.task_id, str(value.worker_id)
            existing = await db.get(DelegationEvidenceReadRow, receipt_id)
            if existing:
                if existing.request_digest != digest:
                    raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "同一 Worker ToolCall 参数冲突")
                artifact = await db.get(ArtifactRow, existing.response_artifact_id)
                if not artifact or not await asyncio.to_thread(store.verify, artifact.relative_key, artifact.sha256):
                    raise ApiError(ErrorCode.CONFLICT, "读取收据内容不可用")
                return json.loads(store.read_bytes(artifact.relative_key))
            source = None
            result: dict[str, Any] = {"ok": True, "receipt_id": str(receipt_id)}
            if value.action == "list":
                stmt = select(ArtifactRow).where(*visible_evidence(parent.id))
                if value.cursor:
                    stmt = stmt.where(ArtifactRow.id > value.cursor)
                if value.query:
                    stmt = stmt.where(ArtifactRow.summary.icontains(value.query, autoescape=True))
                rows = list((await db.scalars(stmt.order_by(ArtifactRow.id).limit(21))).all())
                result.update(artifacts=[source_metadata(item) for item in rows[:20]],
                    next_cursor=str(rows[19].id) if len(rows) > 20 else None,
                    recommended_refs=row.context_refs, note="目录是元数据，不代表已读取正文")
            else:
                source = await db.scalar(select(ArtifactRow).where(ArtifactRow.id == value.artifact_id, *visible_evidence(parent.id)))
                pinned = next((s for s in row.handoff_snapshot.get("context_sources", []) if s["artifact_id"] == str(value.artifact_id)), None)
                if not source or source.availability != "available":
                    result.update(ok=False, error="evidence_not_available")
                elif (value.expected_sha256 and source.sha256 != value.expected_sha256) or (pinned and any(
                        source_metadata(source)[key] != pinned[key] for key in ("sha256", "byte_size", "media_type", "captured_at", "source_operation_id"))):
                    result.update(ok=False, error="source_reference_changed")
                else:
                    try:
                        result["data"] = await asyncio.to_thread(read_view, store, source, value.offset, value.limit)
                    except UnicodeDecodeError:
                        result.update(ok=False, error="invalid_utf8_source")
                    except (ValueError, ArtifactError, OSError) as exc:
                        result.update(ok=False, error=str(exc) if isinstance(exc, ValueError) else "evidence_read_failed")
            guard(parent, run, row, self.settings)
            if result.get("ok") and value.action == "read":
                data = result["data"]
                if data["returned_bytes"]:
                    result["read_ref"] = {"receipt_id": str(receipt_id), **{k: data[k] for k in
                        ("artifact_id", "sha256", "view_sha256", "offset", "returned_bytes", "truncated")}}
            stored = store.write_bytes(json.dumps(result, ensure_ascii=False).encode(), media_type="application/json")
            response = await ArtifactRepository(db).create(artifact_id=uuid5(receipt_id, "response"), sha256=stored.sha256,
                byte_size=stored.byte_size, media_type=stored.media_type, relative_key=stored.relative_key,
                session_id=parent.id, source_operation_id=None, sensitivity="internal", availability="available",
                captured_at=stored.captured_at, captured_by_identity=f"worker-evidence:{row.id}", summary="Worker 证据交付收据（脱敏视图）")
            db.add(DelegationEvidenceReadRow(id=receipt_id, delegation_id=row.id, tool_call_id=value.tool_call_id,
                request_digest=digest, artifact_id=value.artifact_id if value.action == "read" else None,
                source_sha256=source.sha256 if source else None, response_artifact_id=response.id))
            return result


async def validate_result_evidence(db: Any, settings: Settings, row: AgentDelegationRow, result: WorkerResult) -> bool:
    if set(result.evidence_refs) != {str(r.artifact_id) for r in result.evidence_reads}:
        return False
    store = ArtifactStore(settings)
    for ref in result.evidence_reads:
        receipt = await db.get(DelegationEvidenceReadRow, ref.receipt_id)
        if not receipt or receipt.delegation_id != row.id or receipt.artifact_id != ref.artifact_id or receipt.source_sha256 != ref.sha256:
            return False
        response = await db.get(ArtifactRow, receipt.response_artifact_id)
        source = await db.scalar(select(ArtifactRow).where(ArtifactRow.id == ref.artifact_id, *visible_evidence(row.parent_session_id)))
        if not response or not source or source.availability != "available" or source.sha256 != ref.sha256:
            return False
        try:
            if not await asyncio.to_thread(store.verify, response.relative_key, response.sha256):
                return False
            delivered = json.loads(store.read_bytes(response.relative_key))
            if delivered.get("read_ref") != ref.model_dump(mode="json"):
                return False
            if any(delivered.get("data", {}).get(key) != source_metadata(source)[key] for key in
                   ("sha256", "byte_size", "media_type", "captured_at", "source_operation_id")):
                return False
            await asyncio.to_thread(verified_source, store, source)
        except (ArtifactError, OSError, ValueError):
            return False
    return True
