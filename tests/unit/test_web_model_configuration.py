"""真实序列化与本机持久化，不调用供应商或靶场。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage

from agent_workbench.config.catalog import default_provider_profiles
from agent_workbench.config.model_settings import (
    ModelPreferences,
    StoredProvider,
    apply_documented_limits,
    model_secret_path,
    read_model_key,
    save_preferences,
    write_model_key,
)
from agent_workbench.config.secrets import build_secret_store
from agent_workbench.contracts.models import CatalogModel
from agent_workbench.models.context_budget import estimate_request
from agent_workbench.models.provider import (
    ModelBinding,
    build_chat_model,
    request_payload,
    resolve_provider_profile,
    upgrade_legacy_budget,
)
from tests.support.settings import isolated_settings


def test_bailian_snapshot_defaults_cover_full_agent_request_and_worker(tmp_path):
    from agent_workbench.config.worker_model import shared_worker_model
    from agent_workbench.models.context_budget import ensure_fits

    endpoint = "https://ws-fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    cfg = isolated_settings(data_root=tmp_path, secret_store_path=tmp_path / "secrets.json")
    entry = CatalogModel(model_id="deepseek-v4.1-flash-1017")
    provider = StoredProvider(
        provider_id="any-provider",
        display_name="百炼快照",
        credential_slot="any-provider",
        base_url=endpoint,
        models=[entry],
    )
    save_preferences(
        ModelPreferences(
            providers={"any-provider": provider},
            default_provider="any-provider",
            default_model=entry.model_id,
        ),
        cfg,
    )
    profile = resolve_provider_profile(settings=cfg)
    assert profile.context_limit == profile.max_input_tokens == 1_000_000
    assert profile.max_output_tokens == profile.effective_output_limit == 393_216
    messages = [HumanMessage(content="x" * 91_299)]
    estimate = estimate_request(messages, None, [], profile)
    assert estimate.estimated_input_tokens >= 30_433
    assert estimate.allowed_input_tokens == 546_105
    ensure_fits(estimate)
    write_model_key("any-provider", "fixture-only", cfg)
    worker = shared_worker_model(cfg, ModelBinding(profile).to_snapshot())
    assert worker.context_window == 1_000_000 and worker.max_tokens == 393_216
    loaded = apply_documented_limits(entry, endpoint)
    assert loaded.specification_source == "built_in"
    assert loaded.supports_tools == loaded.supports_images == "unknown"
    assert apply_documented_limits(loaded, endpoint) == loaded


def test_documented_specs_preserve_user_values_and_other_gateway_unknowns():
    endpoint = "https://ws-fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    entry = CatalogModel(
        model_id="deepseek-v4.1-flash-1017",
        context_window=200_000,
        max_input_tokens=190_000,
        max_output_tokens=32_768,
        request_output_limit=8_192,
        specification_source="user",
    )
    assert apply_documented_limits(entry, endpoint) == entry
    unknown = CatalogModel(model_id=entry.model_id)
    assert apply_documented_limits(unknown, "https://gateway.example/v1") == unknown
    assert (
        apply_documented_limits(
            unknown, "https://ws-x.cn-beijing.maas.aliyuncs.com.evil.test/compatible-mode/v1"
        )
        == unknown
    )
    low = CatalogModel(model_id=entry.model_id, context_window=8_192)
    assert apply_documented_limits(low, endpoint).context_window == 8_192


def test_known_snapshot_fallback_upgrade_preserves_binding_and_explicit_budgets():
    from agent_workbench.config.catalog import ProviderProfile

    old = ProviderProfile(
        provider_id="bailian22",
        model_name="deepseek-v4.1-flash-1017",
        base_url="https://ws-fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        secret_ref="model:bailian22",
        context_limit=8192,
        output_reserve=1024,
        request_output_limit=1024,
        profile_version="configured-v2",
        provider_extra={"adapter": "generic"},
    )
    updated = upgrade_legacy_budget(old)
    assert updated.context_limit == updated.max_input_tokens == 1_000_000
    assert updated.max_output_tokens == updated.effective_output_limit == 393_216
    for field in ("provider_id", "model_name", "base_url", "secret_ref", "provider_extra"):
        assert getattr(updated, field) == getattr(old, field)
    assert upgrade_legacy_budget(updated) == updated
    explicit = old.model_copy(update={"max_input_tokens": 8192})
    assert upgrade_legacy_budget(explicit) == explicit


@pytest.mark.parametrize("identity", list(default_provider_profiles()))
def test_official_limits_match_actual_request_and_input_reservation(identity):
    cfg = isolated_settings(
        DASHSCOPE_BASE_URL="https://fixture.invalid/compatible-mode/v1",
        # Historical conservative env settings must no longer shrink these four model profiles.
        output_reserve_tokens=8192,
        DASHSCOPE_CONTEXT_LIMIT=131072,
    )
    profile = resolve_provider_profile(*identity, settings=cfg)
    limit = 131072 if identity[0] == "xiaomi" else 393216

    class FixtureSecrets:
        def resolve(self, secret_ref: str) -> str:
            return "fixture-only"

        def exists(self, secret_ref: str) -> bool:
            return True

    model = build_chat_model(ModelBinding(profile=profile), secret_store=FixtureSecrets())
    messages = [HumanMessage(content="离线预算验证")]
    wire = request_payload(model, messages)
    assert wire["max_completion_tokens"] == limit
    assert "max_tokens" not in wire
    assert wire["temperature"] == 1.0
    context_limit = 1_048_576 if identity[0] == "xiaomi" else 1_000_000
    assert profile.context_limit == profile.max_input_tokens == context_limit
    assert profile.max_output_tokens == profile.recovery_output_limit == limit
    budget = estimate_request(messages, None, [], profile, model=model)
    assert budget.output_limit == limit
    assert budget.allowed_input_tokens == int((context_limit - limit) * 0.9)


def test_legacy_budget_upgrade_preserves_identity_endpoint_and_secret():
    official = default_provider_profiles()[("bailian", "deepseek-v4.1-flash")]
    old = official.model_copy(
        update={
            "context_limit": 131072,
            "request_output_limit": 8192,
            "output_reserve": 8192,
            "recovery_output_limit": 16384,
            "profile_version": "1",
            "base_url": "https://fixture.invalid/compatible-mode/v1",
            "secret_ref": "env:DASHSCOPE_API_KEY",
        }
    )
    upgraded = upgrade_legacy_budget(old)
    assert upgraded.context_limit == 1_000_000
    assert upgraded.effective_output_limit == 393216
    assert upgraded.base_url == old.base_url and upgraded.secret_ref == old.secret_ref
    assert upgrade_legacy_budget(upgraded) == upgraded


def test_key_is_persistent_refreshable_and_explicit_delete_does_not_use_old_env(
    tmp_path, monkeypatch
):
    cfg = isolated_settings(secret_store_path=tmp_path / "secrets.json", data_root=tmp_path)
    monkeypatch.setattr("agent_workbench.config.model_settings.get_settings", lambda: cfg)
    monkeypatch.setenv("XIAOMI_API_KEY", "fixture-old-env")
    write_model_key("xiaomi", "fixture-first", cfg)
    store = build_secret_store()
    assert store.resolve("model:xiaomi") == "fixture-first"
    assert store.resolve("env:XIAOMI_API_KEY") == "fixture-first"
    write_model_key("xiaomi", "fixture-updated", cfg)
    assert store.resolve("model:xiaomi") == "fixture-updated"
    if sys.platform == "win32":
        assert "fixture-updated" not in model_secret_path(cfg).read_text(encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_workbench.config.model_settings import read_model_key; "
            "assert read_model_key('xiaomi') == 'fixture-updated'; print('resolved')",
        ],
        env={**os.environ, "WORKBENCH_SECRET_STORE_PATH": str(cfg.secret_store_path)},
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.strip() == b"resolved"
    write_model_key("xiaomi", None, cfg)
    assert read_model_key("xiaomi", cfg) is None
    assert not store.exists("env:XIAOMI_API_KEY")


def test_preferences_are_read_by_new_resolutions_without_settings_cache_clear(tmp_path):
    cfg = isolated_settings(data_root=tmp_path)
    first = resolve_provider_profile(settings=cfg)
    save_preferences(ModelPreferences(default_model="mimo-v2.6-flash"), cfg)
    second = resolve_provider_profile(settings=cfg)
    assert first.model_name == "mimo-v2.6-pro"
    assert second.model_name == "mimo-v2.6-flash"
