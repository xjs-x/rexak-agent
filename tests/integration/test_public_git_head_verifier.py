"""只确认能由两次独立 HTTP 原始工件证明的 Git HEAD 引用暴露。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.enums import ArtifactAvailability, FindingCategory, OperationStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.tool import ArtifactRef, ToolResult
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.reports import create_report_snapshot
from agent_workbench.domain.verifiers import (
    confirm_public_git_head,
    preflight_public_git_head,
)
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    ProjectRepository,
    SessionRepository,
)
from agent_workbench.persistence.tables import (
    FindingRow,
    RunRow,
    SpanRow,
    ToolJobRow,
    ToolOperationRow,
    VerificationRow,
)
from agent_workbench.tools.registry import build_tools
from agent_workbench.tools.verification import verify_public_git_head

pytestmark = pytest.mark.integration
URL = "http://example.test/.git/HEAD"


async def _add_evidence(db, store, session_id, run_id, *, body, identity="anonymous"):
    operation_id, job_id = uuid4(), uuid4()
    db.add(
        ToolOperationRow(
            operation_id=operation_id,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=f"fixture-{operation_id}",
            tool_name="http_request",
            request_digest="a" * 64,
            effect_class="read_only",
            status="succeeded",
            job_id=job_id,
            result_artifact_ids=[],
        )
    )
    db.add(
        ToolJobRow(
            job_id=job_id,
            operation_id=operation_id,
            executor_id="test-executor",
            state="succeeded",
            sync_status="in_sync",
        )
    )
    meta = {
        "url": URL,
        "final_url": URL,
        "status_code": 200,
        "redirect_chain": [],
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "truncated": False,
    }
    refs = []
    for name, content, media_type in (
        ("body", body, "text/plain"),
        ("meta.json", json.dumps(meta).encode("utf-8"), "application/json"),
    ):
        stored = store.write_bytes(content, media_type=media_type)
        row = await ArtifactRepository(db).create(
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            media_type=stored.media_type,
            relative_key=stored.relative_key,
            session_id=session_id,
            source_operation_id=operation_id,
            sensitivity="internal",
            availability="available",
            captured_at=stored.captured_at,
            captured_by_identity=identity,
            source_url=URL,
            summary=f"response.{name}（{stored.byte_size} 字节）",
        )
        refs.append(row.id)
    await db.flush()
    return refs


async def _setup(
    database,
    store,
    unique_name,
    *,
    initial_body,
    recheck_body,
    recheck_identity="verify-git-head-test",
):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="verifier-test",
            name=unique_name,
            target_description="本地模拟目标",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="验证任务",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = RunRow(
            id=uuid4(),
            session_id=session.id,
            status="completed",
            objective="检查 Git HEAD 引用",
            provider_id="test",
            model_name="scripted",
        )
        db.add(run)
        await db.flush()
        initial = await _add_evidence(db, store, session.id, run.id, body=initial_body)
        recheck = await _add_evidence(
            db,
            store,
            session.id,
            run.id,
            body=recheck_body,
            identity=recheck_identity,
        )
        finding = await FindingService(db).propose(
            session_id=session.id,
            title="模型猜测整个仓库泄露",
            description="尚未独立复测",
            category=FindingCategory.INFO_DISCLOSURE,
            evidence_refs=[str(item) for item in initial],
        )
        return session.id, finding.id, initial, recheck


async def test_public_git_head_verifier_confirms_only_precise_claim(
    database, tmp_path, unique_name
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    session_id, finding_id, initial, recheck = await _setup(
        database,
        store,
        unique_name,
        initial_body=b"ref: refs/heads/main\n",
        recheck_body=b"ref: refs/heads/main\n",
    )
    async with database.session() as db:
        await preflight_public_git_head(
            db,
            store,
            session_id=session_id,
            finding_id=finding_id,
            url=URL,
            initial_body_id=initial[0],
            initial_meta_id=initial[1],
        )
    async with database.transaction() as db:
        verification_id = await confirm_public_git_head(
            db,
            store,
            session_id=session_id,
            finding_id=finding_id,
            url=URL,
            initial_body_id=initial[0],
            initial_meta_id=initial[1],
            recheck_body_id=recheck[0],
            recheck_meta_id=recheck[1],
            recheck_identity="verify-git-head-test",
        )
    async with database.session() as db:
        finding = (
            await db.execute(select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one()
        verification = (
            await db.execute(select(VerificationRow).where(VerificationRow.id == verification_id))
        ).scalar_one()
        protected = [await ArtifactRepository(db).get(item) for item in initial + recheck]
    assert finding.status == "confirmed"
    assert finding.title == "公开可读的 Git HEAD 引用"
    assert "整个仓库" not in finding.description
    assert finding.verification_id == verification_id
    assert verification.performed_by == "code"
    assert verification.recheck_evidence_refs == [str(item) for item in recheck]
    assert "未验证仓库对象" in verification.limitations
    assert all(row is not None and row.protected for row in protected)
    report = await create_report_snapshot(database, store, session_id)
    assert report.confirmed_count == 1
    assert "verify_public_git_head" not in {tool.name for tool in build_tools()}


@pytest.mark.parametrize(
    ("initial_body", "recheck_body", "expected"),
    [
        (b"hello world", b"hello world", "确定性判据"),
        (b"ref: refs/heads/main\n", b"not git head", "确定性判据"),
        (b"ref: refs/heads/../../secret\n", b"ref: refs/heads/main\n", "确定性判据"),
    ],
)
async def test_public_git_head_verifier_rejects_nonproof(
    database, tmp_path, unique_name, initial_body, recheck_body, expected
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    session_id, finding_id, initial, recheck = await _setup(
        database,
        store,
        unique_name,
        initial_body=initial_body,
        recheck_body=recheck_body,
    )
    with pytest.raises(ApiError, match=expected):
        async with database.transaction() as db:
            await confirm_public_git_head(
                db,
                store,
                session_id=session_id,
                finding_id=finding_id,
                url=URL,
                initial_body_id=initial[0],
                initial_meta_id=initial[1],
                recheck_body_id=recheck[0],
                recheck_meta_id=recheck[1],
                recheck_identity="verify-git-head-test",
            )
    async with database.session() as db:
        finding = (
            await db.execute(select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one()
        assert finding.status == "candidate"


async def test_public_git_head_verifier_rejects_reused_or_tampered_evidence(
    database, tmp_path, unique_name
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    session_id, finding_id, initial, recheck = await _setup(
        database,
        store,
        unique_name,
        initial_body=b"ref: refs/heads/main\n",
        recheck_body=b"ref: refs/heads/main\n",
    )
    with pytest.raises(ApiError, match="另一次 HTTP 操作"):
        async with database.transaction() as db:
            await confirm_public_git_head(
                db,
                store,
                session_id=session_id,
                finding_id=finding_id,
                url=URL,
                initial_body_id=initial[0],
                initial_meta_id=initial[1],
                recheck_body_id=initial[0],
                recheck_meta_id=initial[1],
                recheck_identity="anonymous",
            )
    async with database.session() as db:
        row = await ArtifactRepository(db).get(recheck[0])
    assert row is not None
    store.resolve(row.relative_key).write_bytes(b"tampered")
    with pytest.raises(ApiError, match="哈希不符"):
        async with database.transaction() as db:
            await confirm_public_git_head(
                db,
                store,
                session_id=session_id,
                finding_id=finding_id,
                url=URL,
                initial_body_id=initial[0],
                initial_meta_id=initial[1],
                recheck_body_id=recheck[0],
                recheck_meta_id=recheck[1],
                recheck_identity="verify-git-head-test",
            )


async def test_public_git_head_tool_uses_isolated_recheck_and_records_result(
    database, tmp_path, unique_name, monkeypatch
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    call_id = "tool-call-1"
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]
    identity = f"verify-git-head-{digest}"
    session_id, finding_id, initial, recheck = await _setup(
        database,
        store,
        unique_name,
        initial_body=b"ref: refs/heads/main\n",
        recheck_body=b"ref: refs/heads/main\n",
        recheck_identity=identity,
    )
    async with database.session() as db:
        rows = [await ArtifactRepository(db).get(item) for item in recheck]
        run_id = (
            await db.execute(select(RunRow.id).where(RunRow.session_id == session_id))
        ).scalar_one()
    assert all(row is not None for row in rows)
    refs = [
        ArtifactRef(
            artifact_id=row.id,
            media_type=row.media_type,
            byte_size=row.byte_size,
            sha256=row.sha256,
            availability=ArtifactAvailability.AVAILABLE,
            source_operation_id=row.source_operation_id,
            summary=row.summary,
        )
        for row in rows
        if row is not None
    ]
    calls = []

    async def fake_http(args, toolbox, nested_call_id):
        calls.append((args, nested_call_id))
        assert toolbox.session_id == session_id
        assert refs[0].source_operation_id is not None
        return ToolResult(
            operation_id=refs[0].source_operation_id,
            status=OperationStatus.SUCCEEDED,
            artifact_refs=refs,
        )

    monkeypatch.setattr("agent_workbench.tools.verification.run_http_request", fake_http)
    progress = []
    toolbox = SimpleNamespace(db=database, session_id=session_id, run_id=run_id, artifacts=store)
    runtime = SimpleNamespace(
        context=SimpleNamespace(toolbox=toolbox, note_progress=progress.append)
    )
    verifier_coroutine = cast(Any, verify_public_git_head).coroutine
    assert verifier_coroutine is not None
    rejected = await verifier_coroutine(
        runtime=runtime,
        tool_call_id=call_id,
        finding_id=str(finding_id),
        url=URL,
        initial_body_artifact_id=str(uuid4()),
        initial_meta_artifact_id=str(initial[1]),
    )
    assert json.loads(rejected)["verified"] is False
    assert calls == []
    raw = await verifier_coroutine(
        runtime=runtime,
        tool_call_id=call_id,
        finding_id=str(finding_id),
        url=URL,
        initial_body_artifact_id=str(initial[0]),
        initial_meta_artifact_id=str(initial[1]),
    )
    payload = json.loads(raw)
    assert payload["verified"] is True
    assert payload["verification_id"]
    assert calls[0][0].identity == identity
    assert calls[0][0].follow_redirects is False
    assert calls[0][0].save_body_artifact is True
    assert calls[0][1] == f"verify-git-head:{digest}"
    assert progress == [f"verification:{payload['verification_id']}"]
    async with database.session() as db:
        spans = (
            (
                await db.execute(
                    select(SpanRow).where(
                        SpanRow.session_id == session_id,
                        SpanRow.name.like("verification.git_head.%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_name = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)
    assert {name: len(items) for name, items in by_name.items()} == {
        "verification.git_head.preflight": 2,
        "verification.git_head.recheck_dispatch": 1,
        "verification.git_head.confirm": 1,
    }
    assert {item.attributes["status"] for item in by_name["verification.git_head.preflight"]} == {
        "failed",
        "completed",
    }
    assert (
        by_name["verification.git_head.recheck_dispatch"][0].operation_id
        == refs[0].source_operation_id
    )
    assert all(span.run_id == run_id and span.duration_ms >= 0 for span in spans)
    with TestClient(create_app()) as client:
        assert client.post("/api/session").status_code == 200
        response = client.get(f"/api/sessions/{session_id}/spans")
        assert response.status_code == 200
        visible = response.json()["spans"]
    assert {
        item["name"] for item in visible if item["name"].startswith("verification.git_head.")
    } == set(by_name)


async def test_public_git_head_tool_keeps_candidate_when_recheck_is_pending(
    database, tmp_path, unique_name, monkeypatch
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    session_id, finding_id, initial, _recheck = await _setup(
        database,
        store,
        unique_name,
        initial_body=b"ref: refs/heads/main\n",
        recheck_body=b"ref: refs/heads/main\n",
    )

    async def pending_http(_args, _toolbox, _call_id):
        return ToolResult.accepted(uuid4(), uuid4(), "远端请求仍在运行")

    monkeypatch.setattr("agent_workbench.tools.verification.run_http_request", pending_http)
    async with database.session() as db:
        run_id = (
            await db.execute(select(RunRow.id).where(RunRow.session_id == session_id))
        ).scalar_one()
    toolbox = SimpleNamespace(db=database, session_id=session_id, run_id=run_id, artifacts=store)
    runtime = SimpleNamespace(
        context=SimpleNamespace(toolbox=toolbox, note_progress=lambda _value: None)
    )
    verifier_coroutine = cast(Any, verify_public_git_head).coroutine
    raw = await verifier_coroutine(
        runtime=runtime,
        tool_call_id="pending-call",
        finding_id=str(finding_id),
        url=URL,
        initial_body_artifact_id=str(initial[0]),
        initial_meta_artifact_id=str(initial[1]),
    )
    assert json.loads(raw)["verified"] is False
    assert json.loads(raw)["recheck_status"] == "accepted"
    async with database.session() as db:
        finding = (
            await db.execute(select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one()
        spans = (
            (
                await db.execute(
                    select(SpanRow).where(
                        SpanRow.session_id == session_id,
                        SpanRow.name.like("verification.git_head.%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert finding.status == "candidate"
    assert {span.name for span in spans} == {
        "verification.git_head.preflight",
        "verification.git_head.recheck_dispatch",
    }
    assert all(span.attributes["status"] == "completed" for span in spans)


async def test_public_git_head_tool_records_failed_confirmation(
    database, tmp_path, unique_name, monkeypatch
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    call_id = "tampered-recheck"
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]
    session_id, finding_id, initial, recheck = await _setup(
        database,
        store,
        unique_name,
        initial_body=b"ref: refs/heads/main\n",
        recheck_body=b"ref: refs/heads/independent\n",
        recheck_identity=f"verify-git-head-{digest}",
    )
    async with database.session() as db:
        run_id = (
            await db.execute(select(RunRow.id).where(RunRow.session_id == session_id))
        ).scalar_one()
        rows = [await ArtifactRepository(db).get(item) for item in recheck]
    assert all(row is not None for row in rows)
    refs = [
        ArtifactRef(
            artifact_id=row.id,
            media_type=row.media_type,
            byte_size=row.byte_size,
            sha256=row.sha256,
            availability=ArtifactAvailability.AVAILABLE,
            source_operation_id=row.source_operation_id,
            summary=row.summary,
        )
        for row in rows
        if row is not None
    ]
    assert rows[0] is not None
    operation_id = refs[0].source_operation_id
    assert operation_id is not None
    store.resolve(rows[0].relative_key).write_bytes(b"tampered")

    async def fake_http(_args, _toolbox, _nested_call_id):
        return ToolResult(
            operation_id=operation_id,
            status=OperationStatus.SUCCEEDED,
            artifact_refs=refs,
        )

    monkeypatch.setattr("agent_workbench.tools.verification.run_http_request", fake_http)
    toolbox = SimpleNamespace(db=database, session_id=session_id, run_id=run_id, artifacts=store)
    runtime = SimpleNamespace(
        context=SimpleNamespace(toolbox=toolbox, note_progress=lambda _value: None)
    )
    verifier_coroutine = cast(Any, verify_public_git_head).coroutine
    assert verifier_coroutine is not None
    raw = await verifier_coroutine(
        runtime=runtime,
        tool_call_id=call_id,
        finding_id=str(finding_id),
        url=URL,
        initial_body_artifact_id=str(initial[0]),
        initial_meta_artifact_id=str(initial[1]),
    )
    assert json.loads(raw)["verified"] is False
    async with database.session() as db:
        finding = (
            await db.execute(select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one()
        spans = (
            (
                await db.execute(
                    select(SpanRow).where(
                        SpanRow.session_id == session_id,
                        SpanRow.name.like("verification.git_head.%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_name = {span.name: span for span in spans}
    assert finding.status == "candidate"
    assert set(by_name) == {
        "verification.git_head.preflight",
        "verification.git_head.recheck_dispatch",
        "verification.git_head.confirm",
    }
    assert by_name["verification.git_head.confirm"].attributes["status"] == "failed"
    assert by_name["verification.git_head.confirm"].error == "ApiError"
