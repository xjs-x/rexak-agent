"""诊断导出不能截断事件、串入其他 run 或把工件元数据当作有效证据。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.diagnostics import collect_run_diagnostic, write_run_diagnostic
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CommandRow,
    EventRow,
    FindingRow,
    ModelCallRow,
    ObservationRow,
    ToolOperationRow,
)

pytestmark = pytest.mark.integration


async def test_diagnostic_is_run_scoped_redacted_paginated_and_hash_checked(
    database, unique_name, tmp_path
):
    store = ArtifactStore(database.settings)
    operation_id = uuid4()
    missing_id = uuid4()
    good = store.write_bytes(b"private-artifact-body-sentinel", media_type="text/plain")
    bad = store.write_bytes(b"original", media_type="text/plain")
    store.resolve(bad.relative_key).write_bytes(b"tampered")
    unrelated = store.write_bytes(b"other-run", media_type="text/plain")
    now = datetime.now(UTC)
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="diagnostic-test",
            name=unique_name,
            target_description="隔离目标",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id, title="诊断测试", scope_snapshot={}, policy_snapshot={}
        )
        selected = await create_run(
            db,
            session_id=task.id,
            objective="检查诊断",
            provider_id="test",
            model_name="scripted",
            config_snapshot={
                "nested": {"api_key": "synthetic-key-secret", "password": "synthetic-pwd"}
            },
        )
        selected.status = "completed"
        await db.flush()
        other = await create_run(
            db,
            session_id=task.id,
            objective="其他运行",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )
        for source in (selected, other):
            db.add(
                CommandRow(
                    session_id=task.id,
                    run_id=source.id,
                    kind="message",
                    content="private-command-secret-sentinel",
                    payload={"password": "private-command-password"},
                    idempotency_key=str(uuid4()),
                    request_digest="c" * 64,
                )
            )
        db.add(
            ToolOperationRow(
                operation_id=operation_id,
                run_id=selected.id,
                session_id=task.id,
                tool_call_id="diagnostic-http",
                tool_name="http_request",
                request_digest="a" * 64,
                effect_class="read_only",
                status="succeeded",
                result_artifact_ids=[str(missing_id), "invalid-reference"],
            )
        )
        artifact_ids = []
        for stored, source in ((good, operation_id), (bad, operation_id), (unrelated, None)):
            identifier = uuid4()
            artifact_ids.append(identifier)
            db.add(
                ArtifactRow(
                    id=identifier,
                    sha256=stored.sha256,
                    byte_size=stored.byte_size,
                    media_type=stored.media_type,
                    relative_key=stored.relative_key,
                    session_id=task.id,
                    source_operation_id=source,
                    captured_at=stored.captured_at,
                )
            )
        db.add(
            ModelCallRow(
                model_call_id=uuid4(),
                logical_call_id=uuid4(),
                run_id=selected.id,
                session_id=task.id,
                phase="agent",
                provider_id="test",
                model_name="scripted",
                usage_status="reported",
                cost_status="unpriced",
                input_tokens=17,
                output_tokens=3,
                submitted_at=now,
                first_token_at=now,
                completed_at=now,
            )
        )
        db.add(
            ModelCallRow(
                model_call_id=uuid4(),
                logical_call_id=uuid4(),
                run_id=selected.id,
                session_id=task.id,
                phase="agent",
                provider_id="test",
                model_name="scripted",
                usage_status="unknown",
                completed_at=now,
                error_code="ModelRequestTimeout",
            )
        )
        db.add(FindingRow(session_id=task.id, title="当前会话候选", status="candidate"))
        db.add(
            ObservationRow(
                session_id=task.id,
                source_run_id=selected.id,
                summary="运行观察",
                artifact_ids=[str(artifact_ids[0])],
                source_operation_id=operation_id,
                observed_at=now,
            )
        )
        db.add(
            ModelCallRow(
                model_call_id=uuid4(),
                logical_call_id=uuid4(),
                run_id=other.id,
                session_id=task.id,
                phase="agent",
                provider_id="test",
                model_name="scripted",
            )
        )
        # 超过旧出口的一万条上限；连续序号中有同会话其他 run 的事件。
        db.add_all(
            [
                EventRow(
                    event_id=uuid4(),
                    session_id=task.id,
                    run_id=selected.id,
                    session_seq=index,
                    type="message.delta" if index == 1 else "usage.updated",
                    occurred_at=now,
                    payload={"delta": "legacy-secret-sentinel"}
                    if index == 1
                    else {
                        "input_tokens": 17,
                        "headers": {"Authorization": "synthetic-bearer-secret"},
                        "text": "password=synthetic-text-secret",
                        "redaction_version": 1,
                    },
                )
                for index in range(1, 10006)
            ]
        )
        db.add(
            EventRow(
                event_id=uuid4(),
                session_id=task.id,
                run_id=other.id,
                session_seq=10006,
                type="run.status",
                occurred_at=now,
                payload={"other": "other-run-event-sentinel"},
            )
        )
        task.event_seq = 10006

    payload = await collect_run_diagnostic(database, selected.id)
    assert payload["schema_version"] == 2
    assert len(payload["events"]) == 10005
    assert payload["events"][-1]["session_seq"] == 10005
    assert payload["snapshot"]["event_history"] == "retained_only"
    assert len(payload["model_calls"]) == 2
    reported = next(row for row in payload["model_calls"] if row["usage_status"] == "reported")
    unknown = next(row for row in payload["model_calls"] if row["usage_status"] == "unknown")
    assert reported["input_tokens"] == 17
    assert reported["first_token_at"] == now.isoformat()
    assert reported["cost_amount"] is None
    assert unknown["input_tokens"] is None and unknown["output_tokens"] is None
    assert len(payload["observations"]) == 1
    assert payload["session_knowledge"]["findings"][0]["status"] == "candidate"
    assert (
        payload["session_knowledge"]["scope"]
        == "current_session_at_export_not_historical_run_result"
    )
    assert len(payload["operations"]) == 1
    assert len(payload["commands"]) == 1
    assert payload["commands"][0]["run_id"] == str(selected.id)
    assert "content" not in payload["commands"][0] and "payload" not in payload["commands"][0]
    assert len(payload["artifacts"]) == 2
    integrity = {row["id"]: row["integrity_verified"] for row in payload["artifacts"]}
    assert integrity == {str(artifact_ids[0]): True, str(artifact_ids[1]): False}
    assert payload["missing_artifact_metadata"] == [str(missing_id)]
    assert payload["invalid_artifact_references"] == ["invalid-reference"]
    output = write_run_diagnostic(tmp_path, payload)
    body = output.read_bytes()
    assert not body.startswith(b"\xef\xbb\xbf")
    serialized = body.decode("utf-8")
    for secret in (
        "synthetic-key-secret",
        "synthetic-pwd",
        "synthetic-bearer-secret",
        "synthetic-text-secret",
        "private-command-secret-sentinel",
        "private-command-password",
        "legacy-secret-sentinel",
        "other-run-event-sentinel",
        "private-artifact-body-sentinel",
    ):
        assert secret not in serialized
    assert json.loads(serialized)["events"][1]["payload"]["input_tokens"] == 17
    assert output.with_suffix(".sha256").read_text().split()[0] == hashlib.sha256(body).hexdigest()
    from evals.runner import Scenario, _collect_result

    result = await _collect_result(
        scenario=Scenario("test", "测试", "自然目标", "/write", expect_paths=["/write"]),
        trial=1,
        run_id=selected.id,
        session_id=task.id,
        duration=1,
        target_delta={"POST /write": 1},
        raw_dir=tmp_path,
        timed_out=False,
        diagnostic=payload,
        counters_status="available_delta_without_environment_reset",
    )
    assert result.artifacts_available == 1
    assert result.unknown_usage_calls == 1
    assert result.expected_paths_missed == []
    assert result.expected_paths_hit == ["POST /write"]


async def test_missing_run_is_explicit(database):
    with pytest.raises(LookupError, match="run 不存在"):
        await collect_run_diagnostic(database, uuid4())
