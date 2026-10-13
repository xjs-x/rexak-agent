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
            "coverage": {
                "checked": checked,
                "registered": len(coverage),
                "blocked": sum(row.status == "blocked" for row in coverage),
                "not_checked": sum(row.status == "not_checked" for row in coverage),
                "note": "分母仅为已登记的资产/入口 × 身份 × 检查类别，不代表网站漏洞覆盖率。",
            },
            "open_leads": [
                redact_text(row.hypothesis)
                for row in leads
                if row.status not in {"confirmed", "disproved", "blocked"}
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
    if snapshot.get("stage"):
        lines.extend(
            [
                "",
                "## 阶段进度与模型解释",
                "",
                _safe_markdown(json.dumps(snapshot["stage"], ensure_ascii=False)),
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
    lines.extend(["## 候选与未决事项", ""])
    lines.extend(
        f"- [{item['status']}] {_safe_markdown(item['title'])}：{_safe_markdown(item['description'])}"
        for item in snapshot["candidates"]
    )
    lines.extend(f"- 未决线索：{_safe_markdown(item)}" for item in snapshot["open_leads"])
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
