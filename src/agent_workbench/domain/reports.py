"""从业务表生成可追溯、脱敏的报告快照。"""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import ErrorCode, Sensitivity
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.events import EventType
from agent_workbench.evidence.artifacts import (
    ArtifactError,
    ArtifactStore,
    StoredArtifact,
    redact_text,
)
from agent_workbench.observability.events import append_event
from agent_workbench.observability.logging import get_logger
from agent_workbench.observability.spans import Span, SpanRecorder
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CoverageRow,
    FindingRow,
    LeadRow,
    LeadWeaknessLinkRow,
    ObservationRow,
    ReportVersionRow,
    RunRow,
    SessionRow,
    VerificationRow,
)

log = get_logger("agent_workbench.reports")


@dataclass(frozen=True, slots=True)
class ReportResult:
    version: int
    markdown_artifact_id: UUID
    html_artifact_id: UUID
    confirmed_count: int
    candidate_count: int


@dataclass(frozen=True, slots=True)
class CapturedReport:
    report_id: UUID
    session_id: UUID
    version: int
    run_id: UUID | None
    snapshot: dict[str, Any]
    artifacts: tuple[tuple[UUID, str, str], ...]
    finding_revisions: dict[UUID, int]


class ReportService:
    """仅在短事务内读取与发布元数据，不持锁读写工件文件。"""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def capture(
        self, session_id: UUID, stage_metadata: dict[str, Any] | None = None
    ) -> CapturedReport:
        # 串行分配版本；报告正文只使用本次捕获的字段和修订号。
        target = (
            await self._s.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        if target is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
        if stage_metadata and stage_metadata.get("stage_key"):
            existing = (
                await self._s.execute(
                    select(ReportVersionRow)
                    .where(
                        ReportVersionRow.session_id == session_id,
                        ReportVersionRow.input_data_revision["stage_key"].astext
                        == str(stage_metadata["stage_key"]),
                    )
                    .order_by(ReportVersionRow.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None and existing.captured_snapshot is not None:
                saved = existing.captured_snapshot
                if existing.generation_status == "failed":
                    existing.generation_status = "pending"
                return CapturedReport(
                    existing.id,
                    session_id,
                    existing.version,
                    existing.run_id,
                    saved["body"],
                    tuple((UUID(item[0]), item[1], item[2]) for item in saved["artifacts"]),
                    {UUID(key): value for key, value in saved["finding_revisions"].items()},
                )

        previous = (
            await self._s.execute(
                select(ReportVersionRow)
                .where(ReportVersionRow.session_id == session_id)
                .order_by(ReportVersionRow.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        version = (previous.version + 1) if previous is not None else 1
        runs = list(
            (
                await self._s.execute(
                    select(RunRow)
                    .where(RunRow.session_id == session_id)
                    .order_by(RunRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        findings = list(
            (
                await self._s.execute(
                    select(FindingRow)
                    .where(FindingRow.session_id == session_id)
                    .order_by(FindingRow.created_at, FindingRow.id)
                )
            )
            .scalars()
            .all()
        )
        coverage = list(
            (
                await self._s.execute(
                    select(CoverageRow)
                    .where(CoverageRow.session_id == session_id)
                    .order_by(CoverageRow.created_at, CoverageRow.id)
                )
            )
            .scalars()
            .all()
        )
        leads = list(
            (
                await self._s.execute(
                    select(LeadRow)
                    .where(LeadRow.session_id == session_id)
                    .order_by(LeadRow.priority.desc(), LeadRow.id)
                )
            )
            .scalars()
            .all()
        )

        confirmed: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        protected: dict[UUID, tuple[UUID, str, str]] = {}
        finding_revisions: dict[UUID, int] = {}
        for finding in findings:
            if finding.status != "confirmed":
                candidates.append(
                    {
                        "title": redact_text(finding.title),
                        "status": finding.status,
                        "description": redact_text(finding.description),
                    }
                )
                continue
            verification = await self._validated_verification(finding)
            refs = list(dict.fromkeys(finding.evidence_refs or []))
            if not refs:
                raise ApiError(ErrorCode.CONFLICT, f"已确认发现 {finding.id} 没有证据引用")
            checked_refs: list[str] = []
            for ref in refs:
                artifact = await self._validated_artifact(session_id, ref)
                protected[artifact.id] = (artifact.id, artifact.relative_key, artifact.sha256)
                checked_refs.append(str(artifact.id))
            confirmed.append(
                {
                    "id": str(finding.id),
                    "title": redact_text(finding.title),
                    "description": redact_text(finding.description),
                    "impact": redact_text(finding.impact or "未填写"),
                    "verification_id": str(verification.id),
                    "method": redact_text(verification.method_version),
                    "limitations": redact_text(verification.limitations or "未记录"),
                    "evidence_refs": checked_refs,
                }
            )
            finding_revisions[finding.id] = finding.revision

        checked = sum(row.status in {"checked_negative", "checked_positive"} for row in coverage)
        link_rows = list(
            (
                await self._s.execute(
                    select(LeadWeaknessLinkRow)
                    .where(LeadWeaknessLinkRow.session_id == session_id)
                    .order_by(LeadWeaknessLinkRow.created_at, LeadWeaknessLinkRow.id)
                )
            )
            .scalars()
            .all()
        )
        observations = (
            (
                await self._s.execute(
                    select(ObservationRow)
                    .where(ObservationRow.session_id == session_id)
                    .order_by(ObservationRow.observed_at, ObservationRow.id)
                )
            )
            .scalars()
            .all()
        )
        observation_sources: dict[str, list[dict[str, Any]]] = {}
        for observation in observations:
            sources = []
            for ref in observation.artifact_ids:
                try:
                    artifact = await self._s.get(ArtifactRow, UUID(ref))
                except ValueError:
                    artifact = None
                usable = (
                    artifact is not None
                    and artifact.session_id == session_id
                    and artifact.availability == "available"
                    and not artifact.truncated
                )
                sources.append(
                    {
                        "artifact_id": ref,
                        "status": "available_at_capture" if usable else "unavailable_or_truncated",
                    }
                )
                if usable and artifact is not None:
                    protected[artifact.id] = (artifact.id, artifact.relative_key, artifact.sha256)
            observation_sources[str(observation.id)] = sources
        snapshot = {
            "session_id": str(session_id),
            "version": version,
            "generated_at": datetime.now(UTC).isoformat(),
            "title": redact_text(target.title),
            "objectives": [redact_text(row.objective) for row in runs],
            "run_statuses": [row.status for row in runs],
            "confirmed": confirmed,
            "candidates": candidates,
            "weaknesses": [
                {
                    "id": str(row.id),
                    "title": redact_text(row.title),
                    "status": row.status,
                    "weakness_kind": row.weakness_kind,
                    "category": row.category,
                    "conditions": [
                        {
                            "key": str(item.get("key")),
                            "statement": redact_text(str(item.get("statement") or "")),
                            "limitations": redact_text(str(item.get("limitations") or "")) or None,
                            "evidence_refs": list(item.get("evidence_refs") or []),
                        }
                        for item in (row.provided_conditions or [])
                    ],
                    "connection_review": _connection_review_view(row),
                }
                for row in findings
            ],
            "weakness_breakdown": _weakness_breakdown(findings),
            "exploration_chains": [
                {
                    "link_id": str(row.id),
                    "lead_id": str(row.lead_id),
                    "weakness_id": str(row.finding_id),
                    "role": row.role,
                    "relation_state": row.relation_state,
                    "condition_key": row.condition_key,
                    "requirement_key": row.requirement_key,
                    "source_weakness_revision": row.source_weakness_revision,
                }
                for row in link_rows
            ],
            "coverage": {
                "checked": checked,
                "registered": len(coverage),
                "blocked": sum(row.status == "blocked" for row in coverage),
                "not_checked": sum(row.status == "not_checked" for row in coverage),
                "note": "分母仅为已登记的资产/入口 × 身份 × 检查类别，不代表网站漏洞覆盖率。",
            },
            # 受阻与暂缓事项保留在报告里：不能靠改状态消除缺口。
            "open_leads": [
                f"[{row.status}] {redact_text(row.hypothesis)}"
                for row in leads
                if row.status not in {"confirmed", "disproved"} and row.merged_into_id is None
            ],
            "open_items": [
                {
                    "lead_id": str(row.id),
                    "hypothesis": redact_text(row.hypothesis),
                    "status": row.status,
                    "blocked_reason": redact_text(row.blocked_reason or "") or None,
                    "resume_condition": redact_text(row.resume_condition or "") or None,
                    "actionability": (row.actionability_assessment or {}).get("state"),
                    "missing_conditions": [
                        str(item.get("description") or item.get("condition_key"))
                        for item in (row.required_conditions or [])
                        if not item.get("satisfied")
                    ],
                }
                for row in leads
                if row.status not in {"confirmed", "disproved"} and row.merged_into_id is None
            ],
            "observations": [
                {
                    "id": str(row.id),
                    "summary": redact_text(row.summary),
                    "artifact_ids": list(row.artifact_ids),
                    "identity": row.identity_label,
                    "sources": observation_sources[str(row.id)],
                    "url": redact_text(row.url or ""),
                }
                for row in observations
            ],
            "stage": stage_metadata,
            "stage_coverage": _stage_coverage(
                stage_metadata,
                observations_last=str(observations[-1].id) if observations else None,
                findings_last=str(findings[-1].id) if findings else None,
                leads_last=str(leads[-1].id) if leads else None,
                domain_versions={
                    "observations": {str(row.id): 1 for row in observations},
                    "findings": {str(row.id): row.revision for row in findings},
                    "leads": {str(row.id): row.revision for row in leads},
                    "links": {str(row.id): row.revision for row in link_rows},
                },
            ),
        }
        latest_run = runs[-1] if runs else None
        report = ReportVersionRow(
            id=uuid4(),
            session_id=session_id,
            run_id=latest_run.id if latest_run else None,
            version=version,
            input_data_revision={
                "findings": {str(row.id): row.revision for row in findings},
                "coverage": {str(row.id): row.revision for row in coverage},
                "leads": {str(row.id): row.revision for row in leads},
                # 阶段签名包含**有效关系版本**：关系变了，旧报告就不代表当前链条。
                "links": {str(row.id): row.revision for row in link_rows},
                "observations": [str(row.id) for row in observations],
                "stage_key": stage_metadata.get("stage_key") if stage_metadata else None,
                "runs": [str(row.id) for row in runs],
            },
            artifact_refs=[str(item) for item in protected],
            generation_status="pending",
            is_historical=True,
            captured_snapshot={
                "body": snapshot,
                "artifacts": [[str(item[0]), item[1], item[2]] for item in protected.values()],
                "finding_revisions": {str(key): value for key, value in finding_revisions.items()},
            },
        )
        self._s.add(report)
        await self._s.flush()
        return CapturedReport(
            report.id,
            session_id,
            version,
            latest_run.id if latest_run else None,
            snapshot,
            tuple(protected.values()),
            finding_revisions,
        )

    async def publish(
        self,
        captured: CapturedReport,
        md: StoredArtifact,
        page: StoredArtifact,
    ) -> ReportResult:
        report = (
            await self._s.execute(
                select(ReportVersionRow)
                .where(
                    ReportVersionRow.id == captured.report_id,
                    ReportVersionRow.generation_status == "pending",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if report is None:
            raise ApiError(ErrorCode.CONFLICT, "报告版本已发布或不再可用")
        if captured.artifacts:
            ids = [item[0] for item in captured.artifacts]
            current_artifacts = (
                (await self._s.execute(select(ArtifactRow).where(ArtifactRow.id.in_(ids))))
                .scalars()
                .all()
            )
            by_id = {row.id: row for row in current_artifacts}
            for artifact_id, key, sha256 in captured.artifacts:
                current = by_id.get(artifact_id)
                if (
                    current is None
                    or current.session_id != captured.session_id
                    or current.availability != "available"
                    or current.truncated
                    or current.relative_key != key
                    or current.sha256 != sha256
                ):
                    raise ApiError(ErrorCode.CONFLICT, f"发布前证据 {artifact_id} 已变化")
        expected_verifications = {
            UUID(item["id"]): UUID(item["verification_id"])
            for item in captured.snapshot["confirmed"]
        }
        for finding_id, revision in captured.finding_revisions.items():
            current = (
                await self._s.execute(
                    select(FindingRow).where(
                        FindingRow.id == finding_id,
                        FindingRow.revision == revision,
                        FindingRow.status == "confirmed",
                    )
                )
            ).scalar_one_or_none()
            if current is None or current.verification_id != expected_verifications[finding_id]:
                raise ApiError(ErrorCode.CONFLICT, f"发布前已确认发现 {finding_id} 已变化")
            await self._validated_verification(current)
        repo = ArtifactRepository(self._s)
        md_row = await repo.create(
            artifact_id=uuid4(),
            sha256=md.sha256,
            byte_size=md.byte_size,
            media_type=md.media_type,
            relative_key=md.relative_key,
            session_id=captured.session_id,
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
            captured_at=md.captured_at,
            summary=f"报告 v{captured.version} Markdown",
        )
        html_row = await repo.create(
            artifact_id=uuid4(),
            sha256=page.sha256,
            byte_size=page.byte_size,
            media_type=page.media_type,
            relative_key=page.relative_key,
            session_id=captured.session_id,
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
            captured_at=page.captured_at,
            summary=f"报告 v{captured.version} HTML",
        )
        await repo.protect([*(item[0] for item in captured.artifacts), md_row.id, html_row.id])
        for finding_id, revision in captured.finding_revisions.items():
            await self._s.execute(
                update(FindingRow)
                .where(
                    FindingRow.id == finding_id,
                    FindingRow.revision == revision,
                    FindingRow.status == "confirmed",
                )
                .values(reported_in_version=captured.version)
            )
        newer = (
            await self._s.execute(
                select(ReportVersionRow.id)
                .where(
                    ReportVersionRow.session_id == captured.session_id,
                    ReportVersionRow.version > captured.version,
                    ReportVersionRow.generation_status == "completed",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        report.is_historical = newer is not None
        if newer is None:
            await self._s.execute(
                update(ReportVersionRow)
                .where(
                    ReportVersionRow.session_id == captured.session_id,
                    ReportVersionRow.version < captured.version,
                    ReportVersionRow.generation_status == "completed",
                )
                .values(is_historical=True)
            )
        report.generation_status = "completed"
        report.markdown_artifact_id = md_row.id
        report.html_artifact_id = html_row.id
        await self._s.flush()
        await append_event(
            self._s,
            session_id=captured.session_id,
            run_id=captured.run_id,
            event_type=EventType.REPORT_READY,
            payload={
                "version": captured.version,
                "markdown_artifact_id": str(md_row.id),
                "html_artifact_id": str(html_row.id),
            },
        )
        return ReportResult(
            captured.version,
            md_row.id,
            html_row.id,
            len(captured.snapshot["confirmed"]),
            len(captured.snapshot["candidates"]),
        )

    async def _validated_verification(self, finding: FindingRow) -> VerificationRow:
        if finding.verification_id is None:
            raise ApiError(ErrorCode.CONFLICT, f"已确认发现 {finding.id} 缺少独立验证记录")
        row = (
            await self._s.execute(
                select(VerificationRow).where(
                    VerificationRow.id == finding.verification_id,
                    VerificationRow.finding_id == finding.id,
                    VerificationRow.conclusion == "confirmed",
                    VerificationRow.performed_by == "code",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ApiError(ErrorCode.CONFLICT, f"已确认发现 {finding.id} 的验证记录不匹配")
        return row

    async def _validated_artifact(self, session_id: UUID, ref: str) -> ArtifactRow:
        try:
            artifact_id = UUID(ref)
        except ValueError as exc:
            raise ApiError(ErrorCode.CONFLICT, f"证据引用不是工件 ID：{ref}") from exc
        row = await ArtifactRepository(self._s).get(artifact_id)
        if (
            row is None
            or row.session_id != session_id
            or row.availability != "available"
            or row.truncated
        ):
            raise ApiError(ErrorCode.CONFLICT, f"证据 {artifact_id} 不可用或已截断")
        return row


def _connection_review_view(row: FindingRow) -> dict[str, Any] | None:
    """用途判断的**脱敏摘要**；不重写历史判断，也不补造链条。"""
    stored = dict(row.connection_review or {})
    if not stored:
        return None
    return {
        "linked_lead_ids": [str(item) for item in (stored.get("linked_lead_ids") or [])],
        "no_connection_note": redact_text(str(stored.get("no_connection_note") or "")) or None,
        "reviewed_content_revision": stored.get("reviewed_content_revision"),
        "stale": stored.get("reviewed_content_revision") != row.revision,
    }


def _weakness_breakdown(findings: list[FindingRow]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    reviewed = 0
    for row in findings:
        kind = row.weakness_kind or "unclassified"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if (
            row.connection_review
            and row.connection_review.get("reviewed_content_revision") == row.revision
        ):
            reviewed += 1
    return {
        "total": len(findings),
        "by_kind": by_kind,
        "with_connection_review": reviewed,
        "note": (
            "弱点是弱点，不等于漏洞：独立缺陷、可供后续利用的条件与未成立组合假设分别计数，"
            "**不能**把弱点数量当作漏洞数量。"
        ),
    }


@contextmanager
def _report_stage(
    spans: SpanRecorder,
    name: str,
    session_id: UUID,
    run_id: UUID | None = None,
) -> Iterator[Span]:
    span = spans.start(name, session_id=session_id, run_id=run_id)
    try:
        yield span
    except (Exception, asyncio.CancelledError) as exc:
        span.finish(error=type(exc).__name__)
        spans.record(span, status="failed")
        raise
    else:
        span.finish()
        spans.record(span, status="completed")


async def create_report_snapshot(
    db: Database,
    store: ArtifactStore,
    session_id: UUID,
    *,
    stage_metadata: dict[str, Any] | None = None,
) -> ReportResult:
    if not stage_metadata:
        return await _create_report_snapshot(db, store, session_id)
    # 专用会话锁防止同一阶段并发渲染；不持业务行锁跨文件 I/O。
    async with db.session() as lock_session:
        key = f"stage-report:{session_id}"
        locked = (
            await lock_session.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}
            )
        ).scalar_one()
        if not locked:
            raise ApiError(ErrorCode.CONFLICT, "阶段报告正在生成，稍后可按原阶段版本重试")
        try:
            return await _create_report_snapshot(
                db, store, session_id, stage_metadata=stage_metadata
            )
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key}
            )


async def _create_report_snapshot(
    db: Database,
    store: ArtifactStore,
    session_id: UUID,
    *,
    stage_metadata: dict[str, Any] | None = None,
) -> ReportResult:
    spans = SpanRecorder()
    span = spans.start("report.generate", session_id=session_id)
    captured: CapturedReport | None = None
    outcome = "failed"
    try:
        with _report_stage(spans, "report.capture", session_id) as stage:
            async with db.transaction() as session:
                captured = await ReportService(session).capture(session_id, stage_metadata)
                existing = await session.get(ReportVersionRow, captured.report_id)
                if (
                    existing is not None
                    and existing.generation_status == "completed"
                    and existing.markdown_artifact_id
                    and existing.html_artifact_id
                ):
                    outcome = "completed"
                    return ReportResult(
                        existing.version,
                        existing.markdown_artifact_id,
                        existing.html_artifact_id,
                        len(captured.snapshot["confirmed"]),
                        len(captured.snapshot["candidates"]),
                    )
            stage.run_id = captured.run_id
        with _report_stage(spans, "report.verify", session_id, captured.run_id):
            for artifact_id, key, sha256 in captured.artifacts:
                try:
                    valid = store.verify(key, sha256)
                except ArtifactError:
                    valid = False
                if not valid:
                    raise ApiError(ErrorCode.CONFLICT, f"证据 {artifact_id} 文件缺失或哈希不符")
        with _report_stage(spans, "report.render", session_id, captured.run_id):
            md = store.write_bytes(
                render_markdown(captured.snapshot).encode("utf-8"),
                media_type="text/markdown",
                sensitivity=Sensitivity.INTERNAL,
            )
            page = store.write_bytes(
                render_html(captured.snapshot).encode("utf-8"),
                media_type="text/html",
                sensitivity=Sensitivity.INTERNAL,
            )
        with _report_stage(spans, "report.publish", session_id, captured.run_id):
            async with db.transaction() as session:
                result = await ReportService(session).publish(captured, md, page)
        outcome = "completed"
        return result
    except (Exception, asyncio.CancelledError) as exc:
        span.error = type(exc).__name__
        if captured is not None:
            async with db.transaction() as session:
                await session.execute(
                    update(ReportVersionRow)
                    .where(
                        ReportVersionRow.id == captured.report_id,
                        ReportVersionRow.generation_status == "pending",
                    )
                    .values(generation_status="failed")
                )
        raise
    finally:
        span.run_id = captured.run_id if captured is not None else None
        span.finish(error=span.error)
        spans.record(span, status=outcome)
        try:
            async with db.transaction() as session:
                await spans.flush(session)
        except Exception as exc:
            log.warning(
                "report_span_persist_failed",
                session_id=str(session_id),
                error_type=type(exc).__name__,
            )


_MARKDOWN_META = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def _safe_markdown(value: str) -> str:
    """目标内容只作为文字出现，不让其生成图片、链接或新标题。"""
    return _MARKDOWN_META.sub(r"\\\1", html.escape(value, quote=False))


def _stage_coverage(
    stage_metadata: dict[str, Any] | None,
    *,
    observations_last: str | None,
    findings_last: str | None,
    leads_last: str | None,
    domain_versions: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """区分"生成时间"与"覆盖到的位置"，并标示跨来源读取到的新记录（§8.2）。"""
    covered = (stage_metadata or {}).get("covered_position") or {}
    claimed = covered.get("latest_domain_records") or {}
    newer: list[str] = []
    versions = covered.get("domain_versions")
    if versions is not None and domain_versions is not None:
        for kind, actual in domain_versions.items():
            old = versions.get(kind) or {}
            if any(key not in old or revision > old[key] for key, revision in actual.items()):
                newer.append(kind)
    elif claimed:
        if observations_last and str(claimed.get("observations")) != observations_last:
            newer.append("observations")
        if findings_last and str(claimed.get("findings")) != findings_last:
            newer.append("findings")
        if leads_last and str(claimed.get("leads")) != leads_last:
            newer.append("leads")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "covered_position": covered or None,
        "atomic_with_domain": False,
        "newer_domain_records_present": bool(newer),
        "newer_domains": newer,
        "note": (
            "生成时间与覆盖位置不同：图与业务数据库不在同一事务，"
            "本快照不是同一时刻的原子截图。跨来源读取出现较新领域记录时会在此标示。"
        ),
    }


def render_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        f"# {_safe_markdown(snapshot['title'])} · 报告 v{snapshot['version']}",
        "",
        f"生成时间：{snapshot['generated_at']}",
        "",
        "本报告仅陈述已登记范围内的测试事实；未检查项不等于安全。",
        "",
        "## 目标与阶段",
        "",
    ]
    lines.extend(f"- {_safe_markdown(item)}" for item in snapshot["objectives"])
    stage = snapshot.get("stage")
    if stage:
        lines.extend(["", "## 阶段进度与当前判断", ""])
        focus = stage.get("current_focus_summary") or {}
        lines.append(
            f"- 当前处理：{_safe_markdown(str(focus.get('intent') or '未记录'))}"
            + (
                f"（步骤 {focus.get('step_id')}，来源 {focus.get('source')}）"
                if focus.get("step_id")
                else ""
            )
        )
        assessment = stage.get("work_assessment") or {}
        if assessment:
            if assessment.get("evaluated_plan_revision") is not None:
                lines.append(
                    f"- 判断对象：旧计划第 {assessment['evaluated_plan_revision']} 版，步骤 "
                    + ", ".join(assessment.get("evaluated_step_ids") or [])
                    + f"；决策 {assessment.get('decision')}，触发 {assessment.get('trigger')}"
                )
            applicable = stage.get("assessment_applies_to_focus", True)
            if not applicable:
                lines.append("- 以下是此前焦点的历史判断，当前焦点尚未记录新判断。")
            lines.append(f"- 这次结果改变了什么：{_safe_markdown(str(assessment.get('changed')))}")
            label = "下一步" if applicable else "此前拟定的下一步"
            lines.append(f"- {label}：{_safe_markdown(str(assessment.get('next_action')))}")
            if assessment.get("uncertainty"):
                lines.append(f"- 不确定性：{_safe_markdown(str(assessment['uncertainty']))}")
            lines.append(
                f"- 判断来源：工具调用 {assessment.get('source_tool_call_id')}"
                f"（第 {assessment.get('revision')} 版）"
            )
        else:
            # 不能由模板自动补成"环境无漏洞"或"已充分覆盖"。
            lines.append("- 尚未记录判断：本阶段没有留下结构化判断，不能据此推断结论。")
        raw_steps = [step for step in (stage.get("goal_progress") or []) if isinstance(step, dict)]
        for step in raw_steps:
            completion = step.get("completion") or {}
            if completion:
                check = completion.get("check") or {}
                lines.append(
                    f"- 步骤 {step.get('step_id')} 结果：{completion.get('outcome')}；"
                    f"检查方式 {check.get('method')}。"
                    + _safe_markdown(str(check.get("scope") or ""))
                )
            elif step.get("legacy_completion"):
                lines.append(f"- 步骤 {step.get('step_id')} 为历史完成，尚未按当前条件复查。")
        steps = [step for step in raw_steps if step.get("status") not in {"done", "dropped"}]
        if steps:
            lines.append("- 当前计划（未完成）：")
            lines.extend(
                f"  - [{step.get('status')}] {step.get('step_id')}: {step.get('description')}"
                for step in steps[:12]
            )
        open_steps = [step for step in raw_steps if step.get("status") == "blocked"]
        if open_steps:
            lines.append("- 未决问题（受阻步骤）：")
            lines.extend(
                f"  - {step.get('step_id')}: {step.get('rationale') or step.get('description')}"
                for step in open_steps[:8]
            )
        lines.append(f"- 触发原因：{stage.get('trigger')}")
        coverage = snapshot.get("stage_coverage") or {}
        covered = coverage.get("covered_position") or {}
        lines.append(f"- 快照生成时间：{coverage.get('generated_at')}")
        lines.append(
            "- 覆盖到的位置："
            + _safe_markdown(json.dumps(covered, ensure_ascii=False))
            + "。恢复点即该 checkpoint 位置；此后的事件不在本次快照内。"
        )
        if coverage.get("newer_domain_records_present"):
            lines.append(
                "- **注意**：捕获期间读取到比快照更新的领域记录："
                + "、".join(coverage.get("newer_domains") or [])
                + "。本快照不是同一时刻的原子截图。"
            )
        lines.extend(
            [
                "",
                "阶段原始元数据（脱敏）：",
                "",
                _safe_markdown(json.dumps(stage, ensure_ascii=False)),
                "",
            ]
        )
    if snapshot.get("observations"):
        lines.extend(["", "## 关键观察与来源", ""])
        for observation in snapshot["observations"]:
            lines.append(
                f"- {_safe_markdown(observation['summary'])}；观察 ID `{observation['id']}`；"
                + "工件："
                + "、".join(f"`{ref}`" for ref in observation["artifact_ids"])
            )
    lines.extend(["", "## 已确认发现", ""])
    if not snapshot["confirmed"]:
        lines.extend(["本版本没有满足独立验证与证据校验条件的已确认发现。", ""])
    for item in snapshot["confirmed"]:
        lines.extend(
            [
                f"### {_safe_markdown(item['title'])}",
                "",
                _safe_markdown(item["description"]),
                "",
                f"影响：{_safe_markdown(item['impact'])}",
                "",
                f"验证：`{item['verification_id']}`（{_safe_markdown(item['method'])}）",
                "",
                f"限制：{_safe_markdown(item['limitations'])}",
                "",
                "证据工件：" + "、".join(f"`{ref}`" for ref in item["evidence_refs"]),
                "",
            ]
        )
    lines.extend(["## 弱点与后续链条", ""])
    breakdown = snapshot.get("weakness_breakdown") or {}
    if breakdown:
        lines.append(
            f"弱点总数 {breakdown.get('total', 0)}；"
            "按类型："
            + "、".join(f"{key}={value}" for key, value in (breakdown.get("by_kind") or {}).items())
            + f"；已有后续用途判断 {breakdown.get('with_connection_review', 0)} 条。"
        )
        lines.append(_safe_markdown(str(breakdown.get("note") or "")))
        lines.append("")
    weaknesses = snapshot.get("weaknesses") or []
    if not weaknesses:
        lines.extend(["尚未登记弱点记录。", ""])
    for item in weaknesses:
        lines.append(
            f"- `{item['id']}` [{item['weakness_kind']}][{item['status']}] "
            f"{_safe_markdown(item['title'])}"
        )
        for condition in item.get("conditions") or []:
            lines.append(
                f"  - 条件 `{condition['key']}`：{_safe_markdown(condition['statement'])}"
                + (
                    f"（限制：{_safe_markdown(condition['limitations'])}）"
                    if condition.get("limitations")
                    else ""
                )
            )
        review = item.get("connection_review")
        if review:
            if review.get("stale"):
                lines.append("  - 以下是此前版本的用途判断，来源变化后需要重新评估。")
            if review.get("linked_lead_ids"):
                lines.append("  - 后续线索：" + "、".join(review["linked_lead_ids"]))
            elif review.get("no_connection_note"):
                lines.append(f"  - 暂未发现连接：{_safe_markdown(review['no_connection_note'])}")
        else:
            lines.append("  - 尚未记录后续用途判断。")
    chains = snapshot.get("exploration_chains") or []
    if chains:
        lines.extend(["", "已知链条（弱点 → 线索 → 弱点）：", ""])
        lines.extend(
            f"- {item['role']}: `{item['weakness_id']}` ↔ 线索 `{item['lead_id']}`"
            f"（{item['relation_state']}"
            + (f"，条件 `{item['condition_key']}`" if item.get("condition_key") else "")
            + "）"
            for item in chains
        )
    lines.extend(
        [
            "",
            "组合限制：两个弱点分别成立**不代表**它们的组合已成立；报告只列出有引用依据的关系，"
            "组合假设保持候选，除非另有独立验证记录。",
            "",
        ]
    )
    lines.extend(["## 候选与未决事项", ""])
    lines.extend(
        f"- [{item['status']}] {_safe_markdown(item['title'])}：{_safe_markdown(item['description'])}"
        for item in snapshot["candidates"]
    )
    lines.extend(f"- 未决线索：{_safe_markdown(item)}" for item in snapshot["open_leads"])
    open_items = snapshot.get("open_items") or []
    if open_items:
        lines.extend(["", "未决事项的限制说明（受阻与暂缓同样保留）：", ""])
        for item in open_items:
            detail = f"- [{item['status']}] {_safe_markdown(item['hypothesis'])}"
            if item.get("blocked_reason"):
                detail += f"；原因：{_safe_markdown(item['blocked_reason'])}"
            if item.get("resume_condition"):
                detail += f"；恢复条件：{_safe_markdown(item['resume_condition'])}"
            if item.get("missing_conditions"):
                detail += "；缺：" + "、".join(
                    _safe_markdown(str(value)) for value in item["missing_conditions"]
                )
            if item.get("actionability"):
                detail += f"（可推进性：{item['actionability']}）"
            lines.append(detail)
    coverage = snapshot["coverage"]
    lines.extend(
        [
            "",
            "## 覆盖范围与限制",
            "",
            f"已检查 {coverage['checked']} / 已登记 {coverage['registered']}；"
            f"受阻 {coverage['blocked']}；未检查 {coverage['not_checked']}。",
            "",
            coverage["note"],
            "",
        ]
    )
    return "\n".join(lines)


def render_html(snapshot: dict[str, Any]) -> str:
    # 独立模板只使用转义文本；即使报告以 text/html 下载，也不执行目标页面内容。
    escaped = html.escape(render_markdown(snapshot))
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'\"><title>测试报告</title>"
        "<style>body{font-family:system-ui;max-width:900px;margin:2rem auto;line-height:1.5}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head>"
        f"<body><pre>{escaped}</pre></body></html>\n"
    )
