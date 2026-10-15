"""启动选择、新任务绑定和暂停恢复使用隔离 PostgreSQL；不调用真实模型。"""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.run_state import set_run_status
from agent_workbench.models.provider import ProviderConfigurationError
from agent_workbench.persistence.tables import RunRow
from agent_workbench.runtime.runner import AgentRunner
from tests.support.settings import isolated_settings

pytestmark = pytest.mark.integration


async def test_selected_provider_drives_new_runs_and_fixed_runtime_binding(
    database,
    unique_name,
    monkeypatch,
):
    cfg = get_settings().model_copy(
        update={
            "default_provider": "bailian",
            "dashscope_base_url": "https://fixture.invalid/compatible-mode/v1",
        }
    )
    monkeypatch.setattr("agent_workbench.api.routes.get_settings", lambda: cfg)
    from agent_workbench.config.model_settings import write_model_key
    write_model_key("bailian", "fixture-bailian-persisted", cfg)
    with TestClient(create_app()) as client:
        csrf = client.post("/api/session").json()["csrf_token"]
        headers = {"X-Workbench-CSRF": csrf}
        project = client.post(
            "/api/projects",
            headers=headers,
            json={
                "name": unique_name,
                "target_description": "本地模型选择验收",
            },
        ).json()
        created = client.post(
            f"/api/projects/{project['id']}/sessions",
            headers=headers,
            json={
                "project_id": project["id"],
                "title": "fixture",
                "initial_objective": "fixture",
            },
        )
        assert created.status_code == 201, created.text
        run = created.json()["run"]
        assert run["provider_id"] == "bailian"
        assert run["model_name"] == "deepseek-v4.1-flash"
        model_settings = client.get("/api/settings/models").json()
        assert model_settings["default_model"] == run["model_name"]
        selected = next(p for p in model_settings["profiles"] if p["provider_id"] == "bailian")
        assert selected["base_url"] == cfg.dashscope_base_url
        assert selected["secret_configured"] is True
        assert "api_key" not in model_settings
        task_id = created.json()["session"]["id"]
        message = client.post(
            f"/api/sessions/{task_id}/messages",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
            },
            json={"content": "fixture followup"},
        )
        assert message.status_code == 202, message.text
        assert message.json()["created_run"] is False
        async with database.transaction() as db:
            await set_run_status(db, run_id=UUID(run["id"]), target=RunStatus.RUNNING)
            await set_run_status(db, run_id=UUID(run["id"]), target=RunStatus.FAILED)
        next_stage = client.post(
            f"/api/sessions/{task_id}/messages",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"content": "fixture next stage"},
        )
        assert next_stage.status_code == 202, next_stage.text
        assert next_stage.json()["created_run"] is True
        async with database.session() as db:
            next_run = await db.get(RunRow, UUID(next_stage.json()["run_id"]))
            assert next_run is not None
            assert next_run.provider_id == "bailian"
            assert next_run.model_name == cfg.dashscope_model

    async with database.session() as db:
        saved = await db.get(RunRow, UUID(run["id"]))
        assert saved is not None
    # 进程默认小米，实际仍按任务的百炼字段装配，避免台账与请求分离。
    runner = AgentRunner(
        database=database, settings=cfg.model_copy(update={"default_provider": "xiaomi"})
    )
    binding = await runner._capture_model_binding(saved)
    assert binding.profile.provider_id == "bailian"
    async with database.session() as db:
        resumed = await db.get(RunRow, saved.id)
        assert resumed is not None
        assert "model_binding" in resumed.config_snapshot
    changed = AgentRunner(
        database=database,
        settings=cfg.model_copy(
            update={
                "dashscope_base_url": "https://changed.invalid/compatible-mode/v1",
                "output_reserve_tokens": 16384,
            }
        ),
    )
    restored = changed._model_binding(resumed)
    assert restored.profile.base_url == binding.profile.base_url
    assert restored.profile.effective_output_limit == binding.profile.effective_output_limit
    assert "api_key" not in restored.to_snapshot()
    resumed.provider_id = "xiaomi"
    with pytest.raises(ProviderConfigurationError, match="不一致"):
        changed._model_binding(resumed)


def test_pending_xiaomi_run_does_not_use_bailian_key(monkeypatch):
    cfg = isolated_settings(
        default_provider="bailian",
        DASHSCOPE_BASE_URL="https://fixture.invalid/compatible-mode/v1",
    )
    monkeypatch.setenv("XIAOMI_API_KEY", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-bailian")
    run = RunRow(provider_id="xiaomi", model_name="mimo-v2.6-pro", config_snapshot={})
    error = AgentRunner(settings=cfg)._precheck_configuration(run)
    assert error is not None
    assert "model:xiaomi" in error[1]
    assert "fixture-bailian" not in error[1]
