"""Shared frontend configuration and authenticated Pi delivery; synthetic keys only."""

import httpx
import pytest

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.persistence.tables import RunRow
from tests.integration.test_worker_evidence import evidence_case  # noqa: F401

pytestmark = pytest.mark.integration


async def test_frontend_save_rotate_delete_and_private_delivery(database, monkeypatch):
    from fastapi.testclient import TestClient

    cfg = get_settings().model_copy(update={"ultra_worker_token": "fixture-worker-service-token-32-characters"})
    monkeypatch.setattr("agent_workbench.api.worker_model.get_settings", lambda: cfg)
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        session = client.post("/api/session").json()
        headers = {"X-Workbench-CSRF": session["csrf_token"]}
        body = {"provider_id": "bailian", "model_name": "deepseek-v4.1-flash",
                "base_url": "https://fixture.invalid/compatible-mode/v1", "set_default": True}
        service = {"Authorization": "Bearer " + cfg.ultra_worker_token}
        assert client.get("/api/internal/worker-model").status_code == 401
        for key in ("fixture-first-key", "fixture-second-key"):
            saved = client.put("/api/settings/models", json={**body, "api_key": key}, headers=headers)
            assert saved.status_code == 200 and key not in saved.text
            delivered = client.get("/api/internal/worker-model", headers=service)
            assert delivered.status_code == 200 and delivered.json()["api_key"] == key
            assert delivered.json()["model"] == saved.json()["default_model"]
            assert delivered.headers["cache-control"] == "no-store"
        deleted = client.put("/api/settings/models", json={**body, "delete_api_key": True}, headers=headers)
        assert deleted.status_code == 200
        assert client.get("/api/internal/worker-model", headers=service).status_code == 409


async def test_delegation_follows_parent_binding_and_rejects_stopped_parent(database, evidence_case, monkeypatch):  # noqa: F811
    from agent_workbench.config.model_settings import write_model_key
    from agent_workbench.models.provider import ModelBinding, resolve_provider_profile

    manager, _, _, delegation, cfg = evidence_case
    cfg = cfg.model_copy(update={"dashscope_base_url": "https://fixture.invalid/compatible-mode/v1"})
    monkeypatch.setattr("agent_workbench.api.worker_model.get_settings", lambda: cfg)
    binding = ModelBinding(resolve_provider_profile("bailian", "deepseek-v4.1-flash", cfg),
                           request_timeout_seconds=123)
    write_model_key("bailian", "fixture-parent-key", cfg)
    monkeypatch.setattr("agent_workbench.api.worker_model.get_database", lambda: database)
    async with database.transaction() as db:
        row = await db.get(RunRow, manager.run_id)
        assert row
        row.config_snapshot = {**row.config_snapshot, "model_binding": binding.to_snapshot()}
    transport = httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + cfg.ultra_worker_token}
        response = await client.get(f"/api/internal/worker-model?delegation_id={delegation}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["model"] == binding.profile.model_name
        assert response.json()["timeout_ms"] == 123000
        async with database.transaction() as db:
            row = await db.get(RunRow, manager.run_id)
            assert row
            row.status = "completed"
        assert (await client.get(f"/api/internal/worker-model?delegation_id={delegation}", headers=headers)).status_code == 409
