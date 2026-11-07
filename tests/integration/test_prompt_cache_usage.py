"""Saved Worker usage is scoped, integrity checked, and separate from Main."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.domain.worker_usage import worker_token_summary
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import AgentDelegationRow
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def test_worker_result_usage_is_scoped_and_legacy_basis_is_read_only(database, unique_name):
    box = (await context_for(database, unique_name)).toolbox
    foreign = (await context_for(database, unique_name + "-foreign")).toolbox
    store = ArtifactStore(database.settings)
    saved = []
    for index, owner in enumerate([box, box, foreign]):
        delegation, context = uuid4(), uuid4()
        task = str(uuid4())
        usage = {"provider": "fixture", "model": "fixture", "calls": 1, "duration_ms": 1,
                 "usage_status": "reported", "input_tokens": 70 if index == 0 else 100,
                 "cache_read_tokens": 30, "cache_write_tokens": 0}
        if index:
            usage["input_token_basis"] = "total_including_cache"
        raw = json.dumps({"task_id": task, "context_id": str(context), "status": "completed",
            "summary": "synthetic", "observations": [], "evidence_refs": [], "unresolved_questions": [],
            "suggested_next_steps": [], "model_usage": usage}).encode("utf-8")
        stored = store.write_bytes(raw, media_type="application/json")
        async with database.transaction() as db:
            artifact = await ArtifactRepository(db).create(artifact_id=uuid4(), sha256=stored.sha256,
                byte_size=stored.byte_size, media_type="application/json", relative_key=stored.relative_key,
                session_id=owner.session_id, captured_at=stored.captured_at, source_operation_id=None,
                sensitivity="internal", availability="available")
            db.add(AgentDelegationRow(id=delegation, parent_session_id=owner.session_id,
                parent_run_id=owner.run_id, parent_tool_call_id=str(delegation), request_digest="0" * 64,
                context_id=context, a2a_task_id=task, objective="synthetic", handoff_snapshot={},
                context_refs=[], authorization_snapshot={}, state="completed", result_artifact_id=artifact.id))
        saved.append((stored.relative_key, raw))
    async with database.session() as db:
        summary = await worker_token_summary(db, database.settings, box.session_id, box.run_id)
    assert summary.input_tokens == 200 and summary.cache_read_tokens == 60
    assert summary.call_count == 2 and summary.unavailable_delegations == 0
    assert all(store.read_bytes(key) == raw for key, raw in saved)
    with TestClient(create_app()) as client:
        assert client.post("/api/session").status_code == 200
        response = client.get(f"/api/sessions/{box.session_id}/usage")
        assert response.status_code == 200, response.text
        assert response.json()["by_agent"]["worker"]["input_tokens"] == 200
        assert response.json()["by_agent"]["main"]["input_tokens"] is None
    # A damaged result is unavailable, not a zero-cost completed call.
    store.resolve(saved[0][0]).write_bytes(b"corrupt fixture")
    async with database.session() as db:
        summary = await worker_token_summary(db, database.settings, box.session_id, box.run_id)
    assert summary.input_tokens == 100 and summary.unavailable_delegations == 1
