"""报告版本必须保留证据链，不能把候选结果写成确认漏洞。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.enums import FindingStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.findings import FindingService, VerificationService
from agent_workbench.domain.reports import ReportService, create_report_snapshot
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.retention import prune_old_events
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    ProjectRepository,
    SessionRepository,
)
from agent_workbench.persistence.tables import EventRow, ReportVersionRow, SpanRow
from agent_workbench.runtime.stopping import FinishGate

pytestmark = pytest.mark.integration


async def _session(database, unique_name):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="report-test",
            name=unique_name,
            target_description="本地测试目标",
            default_scope={},
        )
        row = await SessionRepository(db).create(
            project_id=project.id,
            title="报告 <script>alert(1)</script> ![外链](https://example.invalid/x)",
            scope_snapshot={},
            policy_snapshot={},
        )
        return row.id


async def test_report_versions_export_only_verified_evidence(
    database,
    unique_name,
    tmp_path,
) -> None:
    session_id = await _session(database, unique_name)
    store = ArtifactStore(Settings(data_root=tmp_path))
    evidence = store.write_bytes(
        b"Authorization: Bearer topsecretvalue\nproof", media_type="text/plain"
    )
    async with database.transaction() as db:
        artifact = await ArtifactRepository(db).create(
            sha256=evidence.sha256,
            byte_size=evidence.byte_size,
            media_type=evidence.media_type,
            relative_key=evidence.relative_key,
            session_id=session_id,
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
            captured_at=evidence.captured_at,
        )
        candidate = await FindingService(db).propose(
            session_id=session_id,
            title="尚未核验",
            description="只是模型推测",
        )
        verified = await FindingService(db).propose(
            session_id=session_id,
            title="确认项",
            description="复测成立",
        )
        await VerificationService(db).record(
            finding_id=verified.id,
            method_version="fixture-v1",
            conclusion=FindingStatus.CONFIRMED,
            input_evidence_refs=[str(artifact.id)],
            performed_by="code",
        )

    first = await create_report_snapshot(database, store, session_id)
    assert (first.version, first.confirmed_count, first.candidate_count) == (1, 1, 1)
    async with database.session() as db:
        decision = await FinishGate(db, store).evaluate(
            session_id=session_id,
            proposal={
                "reason": "goal_answered",
                "goal_answer": "已复测",
                "stop_rationale": "目标已回答",
            },
        )
    # V3：已保存的实质弱点必须先做后续用途判断，不能自动结束。
    assert decision.allowed is False
    assert decision.unreviewed_count >= 2
    assert "review_page" in decision.blocking_objects
    async with database.transaction() as db:
        service = FindingService(db)
        for target in (candidate, verified):
            row = await service.get(target.id)
            assert row is not None
            await service.upsert_weakness(
                session_id=session_id,
                weakness_id=target.id,
                expected_revision=row.revision,
                connection_review={"no_connection_note": "暂时没有可推进的后续路线，保留候选"},
            )
    async with database.session() as db:
        decision = await FinishGate(db, store).evaluate(
            session_id=session_id,
            proposal={
                "reason": "goal_answered",
                "goal_answer": "已复测",
                "stop_rationale": "目标已回答",
            },
        )
    assert decision.allowed is True
    async with database.session() as db:
        open_exploration = await FinishGate(db, store).evaluate(
            session_id=session_id,
            proposal={
                "reason": "exploration_converged",
                "goal_answer": "可达范围已检查",
                "stop_rationale": "没有新方向",
            },
        )
    assert open_exploration.allowed is False
    assert "尚未登记任何覆盖" in open_exploration.reasons[0]
    async with database.session() as db:
        missing_login = await FinishGate(db, store).evaluate(
            session_id=session_id,
            proposal={
                "reason": "exploration_converged",
                "goal_answer": "登录区无法继续",
                "stop_rationale": "缺少账号",
                "missing_conditions": ["测试账号"],
            },
        )
    assert missing_login.decision == "continue", "仅结束申请里的缺失条件不能代替领域条件依据"
    second = await create_report_snapshot(database, store, session_id)
    assert second.version == 2
    async with database.session() as db:
        reports = (
            (
                await db.execute(
                    select(ReportVersionRow)
                    .where(
                        ReportVersionRow.session_id == session_id,
                    )
                    .order_by(ReportVersionRow.version)
                )
            )
            .scalars()
            .all()
        )
        cited = await ArtifactRepository(db).get(artifact.id)
        saved_candidate = await FindingService(db).get(candidate.id)
        saved_verified = await FindingService(db).get(verified.id)
        md = await ArtifactRepository(db).get(first.markdown_artifact_id)
        html = await ArtifactRepository(db).get(first.html_artifact_id)
    assert len(reports) == 2
    assert reports[0].is_historical is True and reports[1].is_historical is False
    assert all(row.generation_status == "completed" for row in reports)
    assert cited is not None and cited.protected is True
    assert saved_candidate is not None and saved_candidate.status == "candidate"
    assert saved_verified is not None and saved_verified.reported_in_version == 2
    assert md is not None and html is not None
    markdown = store.read_bytes(md.relative_key).decode("utf-8")
    html_text = store.read_bytes(html.relative_key).decode("utf-8")
    assert str(artifact.id) in markdown
    assert "尚未核验" in markdown and "## 候选与未决事项" in markdown
    assert "<script>" not in html_text
    assert r"\!\[外链\]" in markdown
    assert "topsecretvalue" not in markdown

    # SSE 历史清理不能触及报告版本、原件或引用保护标记。
    async with database.transaction() as db:
        await db.execute(
            update(EventRow).where(EventRow.session_id == session_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=40))
        )
        assert await prune_old_events(
            db, days=30, apply=True, session_id=session_id
        ) >= 1
    async with database.session() as db:
        reports_after = (
            await db.execute(select(ReportVersionRow).where(ReportVersionRow.session_id == session_id))
        ).scalars().all()
        evidence_after = await ArtifactRepository(db).get(artifact.id)
        md_after = await ArtifactRepository(db).get(first.markdown_artifact_id)
    assert len(reports_after) == 2
    assert evidence_after is not None and evidence_after.protected is True
    assert md_after is not None and md_after.protected is True
    assert store.read_bytes(evidence_after.relative_key) == b"Authorization: Bearer topsecretvalue\nproof"
    assert store.read_bytes(md_after.relative_key).decode("utf-8") == markdown


async def test_report_rejects_tampered_evidence_without_publishing(
    database,
    unique_name,
    tmp_path,
) -> None:
    session_id = await _session(database, unique_name)
    store = ArtifactStore(Settings(data_root=tmp_path))
    evidence = store.write_bytes(b"original proof", media_type="text/plain")
    async with database.transaction() as db:
        artifact = await ArtifactRepository(db).create(
            sha256=evidence.sha256,
            byte_size=evidence.byte_size,
            media_type=evidence.media_type,
            relative_key=evidence.relative_key,
            session_id=session_id,
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
            captured_at=evidence.captured_at,
        )
        finding = await FindingService(db).propose(
            session_id=session_id,
            title="待报告",
            description="有独立复测",
        )
        await VerificationService(db).record(
            finding_id=finding.id,
            method_version="fixture-v1",
            conclusion=FindingStatus.CONFIRMED,
            input_evidence_refs=[str(artifact.id)],
            performed_by="code",
        )
    store.resolve(evidence.relative_key).write_bytes(b"tampered")
    async with database.session() as db:
        decision = await FinishGate(db, store).evaluate(
            session_id=session_id,
            proposal={
                "reason": "goal_answered",
                "goal_answer": "已复测",
                "stop_rationale": "目标已回答",
            },
        )
    assert decision.allowed is False
    assert "unverified_findings" in decision.blocking_objects
    with pytest.raises(ApiError, match="哈希不符"):
        await create_report_snapshot(database, store, session_id)
    async with database.session() as db:
        report = (
            await db.execute(
                select(ReportVersionRow).where(
                    ReportVersionRow.session_id == session_id,
                )
            )
        ).scalar_one()
        saved = await ArtifactRepository(db).get(artifact.id)
        spans = (await db.execute(select(SpanRow).where(
            SpanRow.session_id == session_id,
            SpanRow.name.like("report.%"),
        ))).scalars().all()
        by_name = {span.name: span for span in spans}
    assert report.generation_status == "failed"
    assert set(by_name) == {"report.generate", "report.capture", "report.verify"}
    assert by_name["report.capture"].attributes["status"] == "completed"
    assert by_name["report.verify"].attributes["status"] == "failed"
    assert by_name["report.verify"].error == "ApiError"
    assert by_name["report.generate"].attributes["status"] == "failed"
    assert by_name["report.generate"].error == "ApiError"
    assert report.markdown_artifact_id is None
    assert saved is not None and saved.protected is False


async def test_report_publish_failure_records_stage_and_version(
    database,
    unique_name,
    tmp_path,
    monkeypatch,
) -> None:
    session_id = await _session(database, unique_name)
    store = ArtifactStore(Settings(data_root=tmp_path))

    async def fail_publish(self, captured, markdown, page):
        raise RuntimeError("publish unavailable")

    monkeypatch.setattr(ReportService, "publish", fail_publish)
    with pytest.raises(RuntimeError, match="publish unavailable"):
        await create_report_snapshot(database, store, session_id)

    async with database.session() as db:
        report = (await db.execute(select(ReportVersionRow).where(
            ReportVersionRow.session_id == session_id,
        ))).scalar_one()
        spans = (await db.execute(select(SpanRow).where(
            SpanRow.session_id == session_id,
            SpanRow.name.like("report.%"),
        ))).scalars().all()
    by_name = {span.name: span for span in spans}
    assert report.generation_status == "failed"
    assert set(by_name) == {
        "report.generate", "report.capture", "report.verify", "report.render", "report.publish",
    }
    assert all(by_name[name].attributes["status"] == "completed" for name in (
        "report.capture", "report.verify", "report.render",
    ))
    assert by_name["report.publish"].attributes["status"] == "failed"
    assert by_name["report.publish"].error == "RuntimeError"
    assert by_name["report.generate"].attributes["status"] == "failed"


async def test_report_publish_cancellation_marks_version_failed(
    database,
    unique_name,
    tmp_path,
    monkeypatch,
) -> None:
    session_id = await _session(database, unique_name)
    store = ArtifactStore(Settings(data_root=tmp_path))

    async def cancel_publish(self, captured, markdown, page):
        raise asyncio.CancelledError

    monkeypatch.setattr(ReportService, "publish", cancel_publish)
    with pytest.raises(asyncio.CancelledError):
        await create_report_snapshot(database, store, session_id)

    async with database.session() as db:
        report = (await db.execute(select(ReportVersionRow).where(
            ReportVersionRow.session_id == session_id,
        ))).scalar_one()
        spans = (await db.execute(select(SpanRow).where(
            SpanRow.session_id == session_id,
            SpanRow.name.in_(["report.generate", "report.publish"]),
        ))).scalars().all()
    assert report.generation_status == "failed"
    assert {span.name for span in spans} == {"report.generate", "report.publish"}
    assert all(span.error == "CancelledError" for span in spans)
    assert all(span.attributes["status"] == "failed" for span in spans)


async def test_report_api_creates_lists_and_downloads_attachment(
    database,
    unique_name,
) -> None:
    from agent_workbench.api.app import create_app

    session_id = await _session(database, unique_name)
    with TestClient(create_app()) as client:
        ticket = client.post("/api/session")
        assert ticket.status_code == 200
        csrf = ticket.json()["csrf_token"]
        assert client.post(f"/api/sessions/{session_id}/reports").status_code == 403
        response = client.post(
            f"/api/sessions/{session_id}/reports",
            headers={"X-Workbench-CSRF": csrf},
        )
        assert response.status_code == 201, response.text
        assert response.json()["version"] == 1
        spans = client.get(f"/api/sessions/{session_id}/spans").json()["spans"]
        report_span = next(item for item in spans if item["name"] == "report.generate")
        assert report_span["status"] == "completed"
        assert report_span["run_id"] is None
        assert report_span["duration_ms"] >= 0
        report_stages = {
            item["name"]: item for item in spans if item["name"].startswith("report.")
        }
        assert set(report_stages) == {
            "report.generate", "report.capture", "report.verify", "report.render", "report.publish",
        }
        for stage in report_stages.values():
            assert stage["status"] == "completed"
            assert stage["error"] is None
            assert stage["duration_ms"] >= 0
        versions = client.get(f"/api/sessions/{session_id}/reports")
        assert versions.status_code == 200
        assert versions.json()["reports"][0]["version"] == 1
        exported = client.get(f"/api/sessions/{session_id}/reports/1/export?format=html")
        assert exported.status_code == 200
        assert exported.headers["content-disposition"] == 'attachment; filename="report-v1.html"'
        assert exported.headers["x-content-type-options"] == "nosniff"
        assert b"<script>" not in exported.content
