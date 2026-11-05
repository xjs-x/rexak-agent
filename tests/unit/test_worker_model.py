"""独立 Worker 模型配置；不接触日常密钥。"""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_workbench.config import worker_model
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.delegation import DelegationInput


def test_worker_model_snapshot_is_separate_and_encrypted(tmp_path, monkeypatch):
    settings = Settings(secret_store_path=tmp_path / "secrets.json")
    profile = SimpleNamespace(base_url="https://fixture.example/compatible-mode/v1",
                              effective_output_limit=393216, context_limit=1000000)
    monkeypatch.setattr(worker_model, "resolve_provider_profile", lambda *_args: profile)
    monkeypatch.setattr(worker_model, "read_model_key", lambda *_args: "fixture-key-do-not-log")
    assert worker_model.read_worker_model(settings) is None
    worker_model.configure_worker_model(settings, "bailian", "deepseek-v4.1-flash")
    cfg = worker_model.read_worker_model(settings)
    assert cfg and cfg.model == "deepseek-v4.1-flash" and cfg.timeout_ms == 300000
    assert cfg.max_tokens == 393216 and cfg.temperature == 1
    assert "fixture-key" not in repr(cfg)
    monkeypatch.setattr(worker_model, "read_model_key", lambda *_args: "changed-main-key")
    assert cfg == worker_model.read_worker_model(settings)
    assert cfg.transport_value()["api_key"] == "fixture-key-do-not-log"
    import sys

    if sys.platform == "win32":
        assert "fixture-key" not in worker_model.worker_model_path(settings).read_text(encoding="utf-8")
    assert not settings.secret_store_path.exists()


def test_bad_worker_configuration_never_falls_back(tmp_path):
    settings = Settings(secret_store_path=tmp_path / "secrets.json")
    path = worker_model.worker_model_path(settings)
    path.write_text(json.dumps({"format": "invalid", "payload": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        worker_model.read_worker_model(settings)
    with pytest.raises(ValidationError):
        worker_model.WorkerModelConfig.model_validate({"provider": "bailian", "model": "x", "api_key": "",
            "base_url": "http://untrusted.example", "max_tokens": 10, "context_window": 20, "timeout_ms": 100})


@pytest.mark.parametrize("extra", [{"background": "x" * 8001}, {"completion_requirements": [" "]},
                                   {"completion_requirements": ["x"] * 9}, {"context_id": "forged"},
                                   {"context_sources": []}, {"api_key": "forged"}])
def test_handoff_limits_and_backend_owned_fields(extra):
    with pytest.raises(ValidationError):
        DelegationInput.model_validate({"objective": "open exploration", **extra})
    assert DelegationInput(objective="开放探索").completion_requirements == []
