"""只读领域接口的会话隔离、分页及测试条件脱敏。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import FindingStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.findings import FindingService, VerificationService
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import (
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
)
from agent_workbench.persistence.tables import (
    AssetRow,
    LeadRow,
    ModelCallRow,
    RunRow,
    SpanRow,
    ToolOperationRow,
)

pytestmark = pytest.mark.integration


async def test_domain_read_api_is_scoped_paginated_and_redacted(database, unique_name) -> None:
    now = datetime.now(UTC)
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=get_settings().owner_id,
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        sessions = [
            await SessionRepository(db).create(
                project_id=project.id,
                title=f"会话 {index}",
                scope_snapshot={},
                policy_snapshot={},
            )
            for index in range(2)
        ]
        assets = []
        leads = []
        findings = []
        runs = []
        for index, session in enumerate(sessions):
            run = RunRow(
                id=uuid4(),
                session_id=session.id,
                status="completed",
                objective="接口验收",
                provider_id="test",
                model_name="scripted",
            )
            db.add(run)
            runs.append(run)
            for number in range(3):
                await append_event(
                    db,
                    session_id=session.id,
                    event_type=EventType.RUN_STATUS,
                    payload={"status": "completed", "number": number},
                )
            for path in ("/a?token=hiddenvalue", "/b"):
                asset = AssetRow(
                    id=uuid4(),
                    session_id=session.id,
                    scheme="http",
                    host="localhost",
                    port=8080 + index,
                    path=path,
                    method="GET",
                    query_keys=["token"] if path.startswith("/a") else [],
                    identity_refs=[],
                    identity_key="",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(asset)
                assets.append(asset)
                lead = LeadRow(
                    id=uuid4(),
                    session_id=session.id,
                    asset_id=asset.id,
                    hypothesis=f"检查 {path}",
                    identity_refs=[],
                    priority=50,
                    priority_rationale={"score": 50},
                    status="new",
                    evidence_refs=[],
                )
                db.add(lead)
                leads.append(lead)
            finding = await FindingService(db).propose(
                session_id=session.id,
                title="测试发现",
                description="待验证",
            )
            findings.append(finding)
            with pytest.raises(ApiError, match="模型判断不能作为独立验证"):
                await VerificationService(db).record(
                    finding_id=finding.id,
                    method_version="model-only-v1",
                    conclusion=FindingStatus.CONFIRMED,
                    performed_by="model",
                )
            await VerificationService(db).record(
                finding_id=finding.id,
                method_version="read-api-test-v1",
                conclusion=FindingStatus.INCONCLUSIVE,
                tested_conditions={
                    "request": "Authorization: Bearer verysecretvalue",
                    "password": "anothersecretvalue",
                    "result": "未能复现",
                },
                limitations="仅在当前测试条件下有效",
            )
            for number in range(2 if index == 0 else 1):
                operation_id = uuid4()
                job_id = uuid4() if index == 0 and number == 0 else None
                db.add(
                    ToolOperationRow(
                        operation_id=operation_id,
                        run_id=run.id,
                        session_id=session.id,
                        tool_call_id=f"tool-{index}-{number}",
                        tool_name="run_http_request",
                        request_digest="a" * 64,
                        effect_class="read_only",
                        status="succeeded",
                        job_id=job_id,
                    )
                )
                if job_id is not None:
                    await ToolJobRepository(db).upsert(
                        job_id=job_id,
                        operation_id=operation_id,
                        executor_id="test-executor",
                        state="succeeded",
                        executor_accepted_at=now,
                        executor_started_at=now + timedelta(seconds=1),
                        executor_finished_at=now + timedelta(seconds=3),
                    )
                    await ToolJobRepository(db).upsert(
                        job_id=job_id,
                        operation_id=operation_id,
                        executor_id="test-executor",
                        state="succeeded",
                    )
        db.add(
            ModelCallRow(
                model_call_id=uuid4(),
                logical_call_id=uuid4(),
                run_id=runs[0].id,
                session_id=sessions[0].id,
                phase="model_complete",
                provider_id="test",
                model_name="scripted",
                submitted_at=now,
                first_token_at=now,
                completed_at=now,
            )
        )
        db.add_all(
            [
                SpanRow(
                    id=uuid4(),
                    name="finish_gate.middleware",
                    session_id=sessions[index].id,
                    run_id=runs[index].id,
                    started_at=now,
                    finished_at=now + timedelta(milliseconds=12),
                    duration_ms=12,
                    error="secret=hiddenvalue",
                    attributes={"decision": "continue", "secret": "hiddenvalue"},
                )
                for index in range(2)
            ]
        )

    first, second = (session.id for session in sessions)
    with TestClient(create_app()) as client:
        assert client.get(f"/api/sessions/{first}/assets").status_code == 401
        assert client.post("/api/session").status_code == 200

        sessions_page = client.get(f"/api/projects/{project.id}/sessions?limit=1")
        assert sessions_page.status_code == 200
        assert len(sessions_page.json()["sessions"]) == 1
        assert sessions_page.json()["next_offset"] == 1
        assert (
            client.get(f"/api/projects/{project.id}/sessions?limit=1&offset=1")
            .json()["next_offset"]
            is None
        )
        history = client.get(f"/api/sessions/{first}/events/page?limit=2")
        assert [row["session_seq"] for row in history.json()["events"]] == [2, 3]
        assert history.json()["next_before"] == 2
        earlier = client.get(f"/api/sessions/{first}/events/page?before=2&limit=2")
        assert [row["session_seq"] for row in earlier.json()["events"]] == [1]
        assert earlier.json()["next_before"] is None
        assert client.get(f"/api/sessions/{first}/usage?run_id={runs[1].id}").status_code == 404
        model_calls = client.get(f"/api/sessions/{first}/model-calls").json()
        assert len(model_calls["model_calls"]) == 1
        assert model_calls["model_calls"][0]["first_token_at"] is not None
        assert client.get(f"/api/sessions/{second}/model-calls").json()["model_calls"] == []
        operations = client.get(f"/api/sessions/{first}/operations?limit=1")
        assert operations.status_code == 200
        assert len(operations.json()["operations"]) == 1
        assert operations.json()["next_offset"] == 1
        assert operations.json()["operations"][0]["created_at"]
        assert operations.json()["operations"][0]["updated_at"]
        timed = next(
            item for item in client.get(f"/api/sessions/{first}/operations").json()["operations"]
            if item["job_state"] == "succeeded"
        )
        assert datetime.fromisoformat(timed["executor_accepted_at"]) == now
        assert datetime.fromisoformat(timed["executor_started_at"]) == now + timedelta(seconds=1)
        assert datetime.fromisoformat(timed["executor_finished_at"]) == now + timedelta(seconds=3)
        assert len(client.get(f"/api/sessions/{first}/operations?offset=1&limit=1").json()["operations"]) == 1
        assert len(client.get(f"/api/sessions/{second}/operations").json()["operations"]) == 1
        assert client.get(f"/api/sessions/{uuid4()}/operations").status_code == 404
        assert client.get(f"/api/sessions/{first}/operations?limit=201").status_code == 422
        spans = client.get(f"/api/sessions/{first}/spans").json()
        assert len(spans["spans"]) == 1
        assert spans["spans"][0]["duration_ms"] == 12
        assert spans["spans"][0]["decision"] == "continue"
        assert spans["spans"][0]["error"] is None
        assert "hiddenvalue" not in str(spans)
        assert len(client.get(f"/api/sessions/{second}/spans").json()["spans"]) == 1
        assert client.get(f"/api/sessions/{uuid4()}/spans").status_code == 404
        assert client.get(f"/api/sessions/{first}/spans?limit=201").status_code == 422

        page = client.get(f"/api/sessions/{first}/assets?limit=1")
        assert page.status_code == 200, page.text
        assert len(page.json()["assets"]) == 1
        assert page.json()["next_offset"] == 1
        other_page = client.get(f"/api/sessions/{first}/assets?limit=1&offset=1")
        assert len(other_page.json()["assets"]) == 1
        assert other_page.json()["next_offset"] is None
        assert "hiddenvalue" not in page.text + other_page.text
        assert {item["id"] for item in page.json()["assets"] + other_page.json()["assets"]} == {
            str(asset.id) for asset in assets[:2]
        }

        lead_page = client.get(f"/api/sessions/{first}/leads?limit=1")
        assert lead_page.status_code == 200
        assert lead_page.json()["next_offset"] == 1
        assert "hiddenvalue" not in lead_page.text
        assert client.get(f"/api/sessions/{first}/leads?status=blocked").json() == {
            "leads": [],
            "next_offset": None,
        }
        assert client.get(f"/api/sessions/{second}/leads").json()["leads"][0]["id"] in {
            str(lead.id) for lead in leads[2:]
        }

        finding_page = client.get(f"/api/sessions/{first}/findings?limit=1")
        assert finding_page.status_code == 200
        assert [item["id"] for item in finding_page.json()["findings"]] == [
            str(findings[0].id)
        ]
        assert finding_page.json()["next_offset"] is None
        assert client.get(f"/api/sessions/{first}/coverage").status_code == 200

        own = client.get(f"/api/sessions/{first}/findings/{findings[0].id}/verifications")
        assert own.status_code == 200
        assert len(own.json()["verifications"]) == 1
        assert own.json()["verifications"][0]["tested_conditions"]["password"] == "<redacted>"
        assert "verysecretvalue" not in own.text
        assert "anothersecretvalue" not in own.text
        assert (
            client.get(f"/api/sessions/{first}/findings/{findings[1].id}/verifications")
            .status_code
            == 404
        )
        assert client.get(f"/api/sessions/{uuid4()}/assets").status_code == 404
        assert client.get(f"/api/sessions/{uuid4()}/coverage").status_code == 404
        assert client.get(f"/api/sessions/{first}/assets?limit=201").status_code == 422
