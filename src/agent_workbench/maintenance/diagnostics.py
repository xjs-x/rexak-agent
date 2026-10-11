"""单个 run 的脱敏诊断快照；原始正文仍由工件库保存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from agent_workbench import __version__
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore, redact_text
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CommandRow,
    CoverageRow,
    EventRow,
    FindingRow,
    LeadRow,
    ModelCallRow,
    ObservationRow,
    ReportVersionRow,
    RunRow,
    SessionRow,
    SpanRow,
    ToolJobRow,
    ToolOperationRow,
    VerificationRow,
)

_SECRET_FIELD = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|csrf|xsrf|private[_-]?key)"
)
_TOKEN_COUNTERS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    }
)


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _SECRET_FIELD.search(str(key))
                and str(key) != "first_token_at"
                and not (str(key) in _TOKEN_COUNTERS and (item is None or type(item) is int))
                else _safe(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _record(row: Any, fields: str) -> dict[str, Any]:
    return _safe({field: getattr(row, field) for field in fields.split()})


async def collect_run_diagnostic(database: Database, run_id: UUID) -> dict[str, Any]:
    """一致地读取业务台账，分页导出全部保留事件，仅引用本 run 的工件。"""
    async with database.session() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        run = (
            await session.execute(select(RunRow).where(RunRow.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise LookupError(f"run 不存在：{run_id}")
        task = (
            await session.execute(select(SessionRow).where(SessionRow.id == run.session_id))
        ).scalar_one()
        calls = (
            (
                await session.execute(
                    select(ModelCallRow)
                    .where(ModelCallRow.run_id == run_id)
                    .order_by(ModelCallRow.submitted_at)
                )
            )
            .scalars()
            .all()
        )
        operations = (
            (
                await session.execute(
                    select(ToolOperationRow)
                    .where(ToolOperationRow.run_id == run_id)
                    .order_by(ToolOperationRow.created_at, ToolOperationRow.operation_id)
                )
            )
            .scalars()
            .all()
        )
        jobs = (
            (
                await session.execute(
                    select(ToolJobRow)
                    .join(ToolOperationRow, ToolJobRow.job_id == ToolOperationRow.job_id)
                    .where(ToolOperationRow.run_id == run_id)
                    .order_by(ToolJobRow.created_at, ToolJobRow.job_id)
                )
            )
            .scalars()
            .all()
        )
        spans = (
            (
                await session.execute(
                    select(SpanRow)
                    .where(SpanRow.run_id == run_id)
                    .order_by(SpanRow.started_at, SpanRow.id)
                )
            )
            .scalars()
            .all()
        )
        reports = (
            (
                await session.execute(
                    select(ReportVersionRow)
                    .where(ReportVersionRow.run_id == run_id)
                    .order_by(ReportVersionRow.version)
                )
            )
            .scalars()
            .all()
        )
        commands = (
            (
                await session.execute(
                    select(CommandRow)
                    .where(CommandRow.run_id == run_id)
                    .order_by(CommandRow.created_at, CommandRow.id)
                )
            )
            .scalars()
            .all()
        )
        observations = (
            (
                await session.execute(
                    select(ObservationRow)
                    .where(ObservationRow.source_run_id == run_id)
                    .order_by(ObservationRow.observed_at, ObservationRow.id)
                )
            )
            .scalars()
            .all()
        )
        # 领域记录按 session 归属，不能伪称某个旧 run 结束时的历史快照。
        knowledge = {}
        for model, label, fields in (
            (
                LeadRow,
                "leads",
                "id hypothesis status priority blocked_reason resume_condition evidence_refs revision created_at updated_at",
            ),
            (
                FindingRow,
                "findings",
                "id title category status evidence_refs verification_id reported_in_version revision created_at updated_at",
            ),
            (
                CoverageRow,
                "coverage",
                "id asset_id entry_label identity_id check_category status reason evidence_refs revision created_at updated_at",
            ),
        ):
            rows = (
                (
                    await session.execute(
                        select(model)
                        .where(model.session_id == run.session_id)
                        .order_by(model.created_at, model.id)
                    )
                )
                .scalars()
                .all()
            )
            knowledge[label] = [_record(row, fields) for row in rows]
        verifications = (
            (
                await session.execute(
                    select(VerificationRow)
                    .join(FindingRow, VerificationRow.finding_id == FindingRow.id)
                    .where(FindingRow.session_id == run.session_id)
                    .order_by(VerificationRow.created_at, VerificationRow.id)
                )
            )
            .scalars()
            .all()
        )
        knowledge["verifications"] = [
            _record(
                row,
                "id finding_id method_version input_evidence_refs recheck_evidence_refs conclusion limitations tested_conditions performed_by created_at updated_at",
            )
            for row in verifications
        ]
        artifact_ids: set[UUID] = {
            call.response_artifact_id for call in calls if call.response_artifact_id is not None
        }
        invalid_refs: list[str] = []
        for reference in (
            [reference for operation in operations for reference in operation.result_artifact_ids]
            + [reference for report in reports for reference in report.artifact_refs]
            + [reference for observation in observations for reference in observation.artifact_ids]
        ):
            try:
                artifact_ids.add(UUID(str(reference)))
            except ValueError:
                invalid_refs.append(str(reference))
        for report in reports:
            artifact_ids.update(
                identifier
                for identifier in (report.markdown_artifact_id, report.html_artifact_id)
                if identifier is not None
            )
        # 按操作归属补齐尚未回写到 result_artifact_ids 的工件元数据。
        artifacts = (
            (
                await session.execute(
                    select(ArtifactRow)
                    .where(
                        ArtifactRow.id.in_(artifact_ids)
                        | ArtifactRow.source_operation_id.in_(
                            [operation.operation_id for operation in operations]
                        )
                    )
                    .order_by(ArtifactRow.captured_at, ArtifactRow.id)
                )
            )
            .scalars()
            .all()
        )
        events: list[dict[str, Any]] = []
        cursor = 0
        while True:
            batch = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.run_id == run_id, EventRow.session_seq > cursor)
                        .order_by(EventRow.session_seq)
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            if not batch:
                break
            for event in batch:
                record = _record(
                    event,
                    "event_id session_id session_seq run_id type schema_version occurred_at payload",
                )
                if event.type == "message.delta" and event.payload.get("redaction_version") != 1:
                    record["payload"] = {
                        "delta": "[旧流式文本已隐藏，请查看对话归档]",
                        "redaction_status": "legacy_hidden",
                    }
                events.append(record)
            cursor = batch[-1].session_seq

    store = ArtifactStore(database.settings)
    artifact_records = []
    for artifact in artifacts:
        record = _record(
            artifact,
            "id sha256 byte_size media_type relative_key source_operation_id session_id sensitivity availability truncated captured_at captured_by_identity source_url protected summary",
        )
        try:
            record["integrity_verified"] = await asyncio.to_thread(
                store.verify, artifact.relative_key, artifact.sha256
            )
            if not record["integrity_verified"]:
                record["integrity_error"] = "missing_or_hash_mismatch"
        except (ArtifactError, OSError) as exc:
            record["integrity_verified"] = False
            record["integrity_error"] = type(exc).__name__
        artifact_records.append(record)

    return {
        "schema_version": 2,
        "exported_at": datetime.now(UTC).isoformat(),
        "service_version": __version__,
        "snapshot": {
            "database_consistency": "repeatable_read",
            "event_history": "retained_only",
            "artifact_integrity_checked_after_database_snapshot": True,
        },
        "run": _record(
            run,
            "id session_id status completion_reason objective provider_id model_name code_version prompt_version config_snapshot stop_reason created_at updated_at",
        ),
        "session": _record(task, "id project_id thread_id title scope_snapshot policy_snapshot"),
        "session_knowledge": {
            "scope": "current_session_at_export_not_historical_run_result",
            **knowledge,
        },
        "observations": [
            _record(
                row,
                "id source_run_id summary identity_label url artifact_ids source_operation_id observed_at",
            )
            for row in observations
        ],
        "model_calls": [
            _record(
                row,
                "model_call_id logical_call_id retry_index phase provider_id model_name usage_status cost_status cost_amount cost_currency input_tokens output_tokens cache_read_tokens cache_write_tokens reasoning_tokens attempts_observable error_code submitted_at first_token_at completed_at pending_sync response_artifact_id price_snapshot",
            )
            for row in calls
        ],
        "operations": [
            _record(
                row,
                "operation_id run_id tool_call_id tool_name request_digest effect_class policy_revision scope_revision dispatch_control_version identity_id job_id status result_artifact_ids created_at updated_at",
            )
            for row in operations
        ],
        "jobs": [
            _record(
                row,
                "job_id operation_id executor_id state sync_status cancel_confirmed output_cursor executor_accepted_at executor_started_at executor_finished_at last_synced_at created_at updated_at",
            )
            for row in jobs
        ],
        "spans": [
            _record(
                row,
                "id name run_id model_call_id operation_id job_id started_at finished_at duration_ms error attributes",
            )
            for row in spans
        ],
        "reports": [
            _record(
                row,
                "id version run_id generation_status markdown_artifact_id html_artifact_id artifact_refs is_historical created_at",
            )
            for row in reports
        ],
        "commands": [
            _record(row, "id run_id kind status control_version created_at updated_at")
            for row in commands
        ],
        "artifacts": artifact_records,
        "missing_artifact_metadata": sorted(
            str(identifier) for identifier in artifact_ids - {artifact.id for artifact in artifacts}
        ),
        "invalid_artifact_references": _safe(invalid_refs),
        "events": events,
        "redaction": "本包按敏感字段与已知文本规则脱敏，不包含工件正文、完整模型请求或 checkpoint；自由文本中的未知凭据不能保证被识别。原件仍保存在本机工件库。已保留事件不代表从未发生历史清理。",
    }


def write_run_diagnostic(target_dir: Path, payload: dict[str, Any]) -> Path:
    """UTF-8 无 BOM，原子替换单个文件；校验清单可检测读取中的版本不一致。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output = target_dir / "diagnostic.json"
    checksum = hashlib.sha256(body).hexdigest()
    for target, content in (
        (output, body),
        (target_dir / "diagnostic.sha256", f"{checksum}  diagnostic.json\n".encode("ascii")),
    ):
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return output
