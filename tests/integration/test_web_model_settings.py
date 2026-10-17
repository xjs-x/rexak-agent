"""设置、会话选择与阶段快照的 API 验收；密钥只使用合成样本。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.api.auth import COOKIE_NAME
from agent_workbench.persistence.tables import RunRow

pytestmark = pytest.mark.integration


def body(model="mimo-v2.6-flash", **kwargs):
    return {
        "provider_id": "xiaomi",
        "model_name": model,
        "base_url": "https://fixture.invalid/v1",
        "api_key": "fixture-web-secret",
        **kwargs,
    }


async def test_model_settings_protect_write_and_do_not_return_keys(database):
    with TestClient(create_app()) as client:
        assert client.get("/api/settings/models").status_code == 401
        ticket = client.post("/api/session").json()
        headers = {"X-Workbench-CSRF": ticket["csrf_token"]}
        assert client.put("/api/settings/models", json=body()).status_code == 403
        assert (
            client.put(
                "/api/settings/models",
                json=body(),
                headers={**headers, "Origin": "https://untrusted.invalid"},
            ).status_code
            == 403
        )
        saved = client.put("/api/settings/models", json=body(), headers=headers)
        assert saved.status_code == 200, saved.text
        assert "fixture-web-secret" not in saved.text
        assert saved.json()["default_model"] == "mimo-v2.6-flash"
        profiles = saved.json()["profiles"]
        assert len(profiles) == 4
        assert all(p["secret_configured"] for p in profiles if p["provider_id"] == "xiaomi")
        assert {p["allowed_input_tokens"] for p in profiles} == {825753, 546105}
        invalid = client.put(
            "/api/settings/models", json=body(api_key=["fixture-private-invalid"]), headers=headers
        )
        assert invalid.status_code == 422 and "fixture-private-invalid" not in invalid.text
        oversized = client.put(
            "/api/settings/models", json=body(api_key="fixture-" * 1024), headers=headers
        )
        assert oversized.status_code == 422 and "fixture-" not in oversized.text
        deleted = client.put(
            "/api/settings/models", json=body(api_key=None, delete_api_key=True), headers=headers
        )
        assert deleted.status_code == 200
        assert not any(
            p["secret_configured"]
            for p in deleted.json()["profiles"]
            if p["provider_id"] == "xiaomi"
        )


async def test_session_reuse_keeps_earlier_pages_csrf_valid(database):
    """同一浏览器多个控制台页面各自获取会话时，后到的调用不得作废先前的 CSRF。

    回归：此前每次 POST /api/session 都签发新会话并覆盖共享 Cookie，其它页面
    内存中的令牌随即失效，「保存模型设置」等写操作一直 403。
    """
    with TestClient(create_app()) as client:
        first = client.post("/api/session").json()
        assert first["reused"] is False
        cookie_after_first = client.cookies.get(COOKIE_NAME)
        second = client.post("/api/session").json()
        assert second["reused"] is True
        assert second["csrf_token"] == first["csrf_token"]
        assert client.cookies.get(COOKIE_NAME) == cookie_after_first
        saved = client.put(
            "/api/settings/models",
            json=body(api_key=None),
            headers={"X-Workbench-CSRF": first["csrf_token"]},
        )
        assert saved.status_code == 200, saved.text


async def test_session_issues_new_ticket_only_without_valid_cookie(database):
    with TestClient(create_app()) as client:
        first = client.post("/api/session").json()
        assert first["reused"] is False
        client.cookies.clear()
        second = client.post("/api/session").json()
        assert second["reused"] is False
        assert second["csrf_token"] != first["csrf_token"]


async def test_session_reuse_does_not_weaken_csrf_check(database):
    """幂等复用只改变签发时机；令牌与 Cookie 不匹配仍然拒绝。"""
    with TestClient(create_app()) as client:
        stale = client.post("/api/session").json()
        client.cookies.clear()
        fresh = client.post("/api/session").json()
        rejected = client.put(
            "/api/settings/models",
            json=body(api_key=None),
            headers={"X-Workbench-CSRF": stale["csrf_token"]},
        )
        assert rejected.status_code == 403
        accepted = client.put(
            "/api/settings/models",
            json=body(api_key=None),
            headers={"X-Workbench-CSRF": fresh["csrf_token"]},
        )
        assert accepted.status_code == 200, accepted.text


async def test_chat_model_selection_is_frozen_for_stage_and_changes_for_new_stage(
    database, unique_name
):
    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        assert client.put("/api/settings/models", json=body(), headers=headers).status_code == 200
        project = client.post(
            "/api/projects",
            json={"name": unique_name, "target_description": "离线测试", "settings_version": "1"},
            headers=headers,
        ).json()
        project_id = project["id"]
        created = client.post(
            f"/api/projects/{project_id}/sessions",
            json={
                "project_id": project_id,
                "title": "配置快照",
                "initial_objective": "离线协议验证",
                "provider_id": "xiaomi",
                "model_name": "mimo-v2.6-pro-ultraspeed",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        session_id, run_id = created.json()["session"]["id"], created.json()["run"]["id"]
        assert (
            client.put(
                "/api/settings/models", json=body("mimo-v2.6-pro"), headers=headers
            ).status_code
            == 200
        )
        mismatch = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "补充", "provider_id": "xiaomi", "model_name": "mimo-v2.6-pro"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert mismatch.status_code == 409
        accepted = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "补充"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert accepted.status_code == 202, accepted.text
        snapshot = client.get(f"/api/sessions/{session_id}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["runs"][-1]["model_name"] == "mimo-v2.6-pro-ultraspeed"
    async with database.transaction() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
        assert run.model_name == "mimo-v2.6-pro-ultraspeed"
        assert run.config_snapshot["model_binding"]["request_output_limit"] == 131072
        run.status = "completed"
    with TestClient(create_app()) as client:
        headers = {
            "X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"],
            "Idempotency-Key": str(uuid4()),
        }
        result = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "下一阶段", "provider_id": "xiaomi", "model_name": "mimo-v2.6-pro"},
            headers=headers,
        )
        assert result.status_code == 202, result.text
        assert result.json()["created_run"]
    async with database.session() as db:
        run = (
            await db.execute(select(RunRow).where(RunRow.id == result.json()["run_id"]))
        ).scalar_one()
        assert run.model_name == "mimo-v2.6-pro"


async def test_paused_legacy_budget_is_upgraded_once_with_audit_without_running_graph(
    database, unique_name
):
    from agent_workbench.config.settings import get_settings
    from agent_workbench.persistence.tables import EventRow
    from agent_workbench.runtime.runner import AgentRunner

    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        project = client.post(
            "/api/projects",
            json={"name": unique_name, "target_description": "离线预算升级"},
            headers=headers,
        ).json()
        created = client.post(
            f"/api/projects/{project['id']}/sessions",
            json={
                "project_id": project["id"],
                "title": "旧预算恢复",
                "initial_objective": "离线预算升级",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["run"]["id"]
    async with database.transaction() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
        snapshot = dict(run.config_snapshot["model_binding"])
        snapshot.update(
            context_limit=131072,
            output_reserve=8192,
            request_output_limit=8192,
            recovery_output_limit=16384,
            profile_version="1",
            secret_ref="env:XIAOMI_API_KEY",
        )
        run.config_snapshot = {**run.config_snapshot, "model_binding": snapshot}
        run.status = "paused"
    runner = AgentRunner(settings=get_settings(), database=database)
    async with database.session() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
    binding = await runner._capture_model_binding(run)
    assert binding.profile.effective_output_limit == 131072
    assert binding.profile.base_url == snapshot["base_url"]
    assert binding.profile.secret_ref == snapshot["secret_ref"]
    await runner._capture_model_binding(run)
    async with database.session() as db:
        events = (
            (await db.execute(select(EventRow).where(EventRow.run_id == run.id))).scalars().all()
        )
        upgrades = [
            event for event in events if event.payload.get("reason") == "official_profile_upgrade"
        ]
        assert len(upgrades) == 1
        assert upgrades[0].payload["previous"]["request_output_limit"] == 8192
        persisted = await db.get(RunRow, run.id)
        assert persisted.status == "paused"
        assert persisted.config_snapshot["model_binding"]["context_limit"] == 1_048_576


async def test_unconfigured_launcher_runner_waits_and_notices_new_key_without_restart(
    database, unique_name, monkeypatch
):
    from agent_workbench.config.settings import get_settings
    from agent_workbench.runtime.runner import AgentRunner

    available = {}
    monkeypatch.setattr(
        "agent_workbench.config.model_settings.read_model_key",
        lambda provider, _settings=None: available.get(provider),
    )
    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        project = client.post(
            "/api/projects",
            json={"name": unique_name, "target_description": "配置前等待"},
            headers=headers,
        ).json()
        response = client.post(
            f"/api/projects/{project['id']}/sessions",
            json={
                "project_id": project["id"],
                "title": "配置前等待",
                "initial_objective": "离线",
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run"]["id"]
    runner = AgentRunner(settings=get_settings(), database=database, production_scan=True)
    assert not any(str(run.id) == run_id for run in await runner._candidate_runs(limit=10000))
    available["xiaomi"] = "fixture-ready"
    assert any(str(run.id) == run_id for run in await runner._candidate_runs(limit=10000))
    available.clear()
    async with database.transaction() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
        assert run.status == "queued" and run.lease_until is None
        run.status = "cancelling"
    assert any(str(run.id) == run_id for run in await runner._candidate_runs(limit=10000))
