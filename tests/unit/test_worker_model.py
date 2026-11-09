"""Main / Worker 共用配置与父任务绑定；仅使用合成密钥。"""


import pytest
from pydantic import ValidationError

from agent_workbench.config import worker_model
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.delegation import DelegationInput


def test_worker_model_follows_shared_settings_and_ignores_legacy_snapshot(tmp_path, monkeypatch):
    from agent_workbench.config.model_settings import ModelPreferences, save_preferences
    settings = Settings(secret_store_path=tmp_path / "secrets.json").model_copy(update={"dashscope_base_url": "https://fixture.invalid/compatible-mode/v1", "xiaomi_base_url": "https://fixture.invalid/v1"})
    worker_model.worker_model_path(settings).write_text('{"format":"obsolete"}', encoding="utf-8")
    save_preferences(ModelPreferences(default_provider="bailian", default_model="deepseek-v4.1-flash"), settings)
    key = ["fixture-key-do-not-log"]
    monkeypatch.setattr(worker_model, "read_model_key", lambda *_args: key[0])
    cfg = worker_model.read_worker_model(settings)
    assert cfg.model == "deepseek-v4.1-flash" and cfg.timeout_ms == 300000
    assert cfg.max_tokens == 393216 and "fixture-key" not in repr(cfg)
    key[0] = "changed-main-key"
    assert worker_model.read_worker_model(settings).api_key.get_secret_value() == key[0]
    worker_model.configure_worker_model(settings, "xiaomi", "mimo-v2.6-pro")
    assert worker_model.read_worker_model(settings).provider == "xiaomi"
    assert worker_model.worker_model_path(settings).read_text(encoding="utf-8") == '{"format":"obsolete"}'


def test_shared_worker_keeps_parent_binding_but_reads_current_key(tmp_path, monkeypatch):
    from agent_workbench.models.provider import ModelBinding, resolve_provider_profile
    settings = Settings(secret_store_path=tmp_path / "secrets.json").model_copy(update={"dashscope_base_url": "https://fixture.invalid/compatible-mode/v1", "xiaomi_base_url": "https://fixture.invalid/v1"})
    binding = ModelBinding(resolve_provider_profile("bailian", "deepseek-v4.1-flash", settings),
                           temperature=0.7, request_timeout_seconds=123)
    monkeypatch.setattr(worker_model, "read_model_key", lambda *_args: "new-shared-key")
    cfg = worker_model.shared_worker_model(settings, binding.to_snapshot())
    assert cfg.model == binding.profile.model_name and cfg.base_url == binding.profile.base_url
    assert cfg.temperature == 0.7 and cfg.timeout_ms == 123000
    monkeypatch.setattr(worker_model, "read_model_key", lambda *_args: None)
    with pytest.raises(ValueError):
        worker_model.shared_worker_model(settings, binding.to_snapshot())


@pytest.mark.parametrize("extra", [{"background": "x" * 8001}, {"completion_requirements": [" "]},
                                   {"completion_requirements": ["x"] * 9}, {"context_id": "forged"},
                                   {"context_sources": []}, {"api_key": "forged"}])
def test_handoff_limits_and_backend_owned_fields(extra):
    with pytest.raises(ValidationError):
        DelegationInput.model_validate({"objective": "open exploration", **extra})
    assert DelegationInput(objective="开放探索").completion_requirements == []
