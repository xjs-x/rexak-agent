"""Web 模型偏好与本机密钥；每次读取磁盘，使 API 与 Runner 无需重启即可共享配置。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sys
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from agent_workbench.config.catalog import default_provider_profiles, documented_model_limits
from agent_workbench.config.secrets import write_secret_file
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.models import CatalogModel, ModelConnection, ProviderDraft

KEY_ENV = {"xiaomi": "XIAOMI_API_KEY", "bailian": "DASHSCOPE_API_KEY"}
configuration_lock = RLock()


class StoredProvider(ProviderDraft):
    credential_slot: str
    adapter: Literal["xiaomi", "bailian", "generic"] = "generic"


class ModelPreferences(BaseModel):
    default_provider: str = "xiaomi"
    default_model: str = "mimo-v2.6-pro"
    endpoints: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, StoredProvider] | None = None
    retired_provider_ids: set[str] = Field(default_factory=set)
    revision: int = 0


def preferences_path(settings: Settings) -> Path:
    return settings.data_root / "settings" / "models.json"


def apply_documented_limits(entry: CatalogModel, endpoint: str) -> CatalogModel:
    """Fill missing specifications without replacing explicit user or API values."""
    limits = documented_model_limits(endpoint, entry.model_id)
    if not limits:
        return entry
    context = entry.context_window or limits["context_window"]
    changes: dict[str, object] = {
        field: value
        for field, value in limits.items()
        if getattr(entry, field) is None and (field == "context_window" or value <= context)
    }
    if entry.request_output_limit is None and entry.specification_source in {"unknown", "built_in"}:
        context = entry.context_window or limits["context_window"]
        maximum = entry.max_output_tokens or limits["max_output_tokens"]
        changes["request_output_limit"] = min(maximum, context - 1)
    if changes:
        if entry.specification_source == "unknown":
            changes["specification_source"] = "built_in"
        return CatalogModel.model_validate(entry.model_dump() | changes)
    return entry


def read_preferences(settings: Settings | None = None) -> ModelPreferences:
    cfg = settings or get_settings()
    path = preferences_path(cfg)
    preferences = (
        ModelPreferences.model_validate_json(path.read_text(encoding="utf-8"))
        if path.exists()
        else ModelPreferences(
            default_provider=cfg.default_provider,
            default_model=cfg.model_for_provider(),
        )
    )
    if preferences.providers is None:
        preferences.providers = {}
        for (pid, model), profile in default_provider_profiles().items():
            provider = preferences.providers.get(pid)
            if provider is None:
                provider = StoredProvider(
                    provider_id=pid,
                    credential_slot=pid,
                    display_name="小米" if pid == "xiaomi" else "百炼",
                    adapter="xiaomi" if pid == "xiaomi" else "bailian",
                    base_url=preferences.endpoints.get(pid)
                    or (cfg.xiaomi_base_url if pid == "xiaomi" else cfg.dashscope_base_url),
                )
                preferences.providers[pid] = provider
            provider.models.append(
                CatalogModel(
                    model_id=model,
                    context_window=profile.context_limit,
                    max_input_tokens=profile.max_input_tokens,
                    max_output_tokens=profile.max_output_tokens,
                    request_output_limit=profile.effective_output_limit,
                    specification_source="built_in",
                )
            )
    for provider in preferences.providers.values():
        provider.models = [
            apply_documented_limits(entry, provider.base_url) for entry in provider.models
        ]
    return preferences


def validate_model_url(endpoint: str, *, inference: bool = False) -> str:
    endpoint = endpoint.strip()
    message = "请填写有效的 API Base URL，使用 HTTPS 或本机 HTTP 地址，不能包含凭据、查询参数或片段"
    if any(char.isspace() or ord(char) < 32 or char in "\\{}?#" for char in endpoint):
        raise ValueError(message)
    try:
        parsed = urlsplit(endpoint)
        url = httpx.URL(endpoint)
        _ = parsed.port
    except (ValueError, httpx.InvalidURL):
        raise ValueError(message) from None
    if (
        not url.host
        or "@" in parsed.netloc
        or "%" in parsed.netloc
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.host in {"127.0.0.1", "::1", "localhost"})
        )
    ):
        raise ValueError(message)
    normalized = str(url)
    if inference:
        normalized = normalized.rstrip("/")
        if normalized.endswith("/chat/completions"):
            normalized = normalized.removesuffix("/chat/completions")
        elif normalized.endswith(("/responses", "/messages", "/models")):
            raise ValueError("请填写 OpenAI Chat Completions API Base URL")
    return normalized


def validate_endpoint(_provider: str, endpoint: str) -> str:
    return validate_model_url(endpoint, inference=True)


def model_origin(endpoint: str) -> tuple[str, str, int]:
    url = httpx.URL(endpoint)
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


def discovery_url(connection: ModelConnection, base_url: str) -> str:
    if connection.models_url:
        return validate_model_url(connection.models_url)
    if connection.discovery_format == "dashscope":
        raise ValueError("DashScope 模型目录需要填写独立的模型列表 API URL")
    return base_url + "/models"


def validate_api_key(key: str) -> str:
    if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("请输入有效的 API Key，不能包含空白字符或非 ASCII 字符")
    return key


def model_secret_path(settings: Settings | None = None) -> Path:
    # 与原目标凭据隔离，沿用不随运行工件备份的本机 secret 目录。
    return (settings or get_settings()).secret_store_path.with_name("model-secrets.json")


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    """Windows 当前登录用户的 DPAPI；密文复制到另一用户/主机不可直接使用。"""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("本机模型密钥加密存储不可用")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def read_model_key(provider: str, settings: Settings | None = None) -> str | None:
    cfg = settings or get_settings()
    path = model_secret_path(cfg)
    if path.exists():
        values = json.loads(path.read_text(encoding="utf-8"))
        stored = values.get(provider)
        if stored == "deleted":
            return None
        if stored:
            if stored.startswith("dpapi:"):
                if sys.platform != "win32":
                    raise OSError("Windows 加密的模型密钥只能由原 Windows 用户读取")
                return _dpapi(base64.b64decode(stored[6:]), decrypt=True).decode("utf-8")
            if stored.startswith("local:") and sys.platform != "win32":
                return stored[6:]
            raise ValueError("本机模型密钥格式无效")
    # 兼容 CLI/.env；Web 已保存的密钥优先，更新后新请求不再读取旧环境值。
    env = KEY_ENV.get(provider)
    if env is None:
        return None
    return (
        os.environ.get(env)
        or (cfg.xiaomi_api_key if provider == "xiaomi" else cfg.dashscope_api_key)
    ) or None


def configured_model_refs(settings: Settings) -> list[str]:
    refs = [f"model:{pid}" for pid in KEY_ENV if read_model_key(pid, settings)]
    refs.extend(f"env:{env}" for pid, env in KEY_ENV.items() if f"model:{pid}" in refs)
    path = model_secret_path(settings)
    if path.exists():
        values = json.loads(path.read_text(encoding="utf-8"))
        refs.extend(
            f"model:{slot}"
            for slot, stored in values.items()
            if "/" in slot and stored != "deleted"
        )
    return refs


def model_ref_matches(provider_id: str, ref: str) -> bool:
    legacy_env = KEY_ENV.get(provider_id)
    if ref == f"model:{provider_id}" or (legacy_env is not None and ref == f"env:{legacy_env}"):
        return True
    return re.fullmatch(rf"model:{re.escape(provider_id)}/[a-f0-9]{{32}}", ref) is not None


def write_model_key(provider: str, value: str | None, settings: Settings) -> None:
    path = model_secret_path(settings)
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if value is None:
        # 空字符串为显式删除标记，禁止回退到残留环境 Key。
        values[provider] = "deleted"
    else:
        values[provider] = (
            "dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"))).decode("ascii")
            if sys.platform == "win32"
            else "local:" + value
        )
    write_secret_file(path, values)


def save_preferences(preferences: ModelPreferences, settings: Settings) -> None:
    path = preferences_path(settings)
    backup = path.with_name("models.v1.backup.json")
    if preferences.providers is not None and path.exists() and not backup.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("providers") is None:
            write_secret_file(backup, previous)
    write_secret_file(path, preferences.model_dump(mode="json"))


def save_provider(
    preferences: ModelPreferences,
    draft: ProviderDraft,
    *,
    previous: StoredProvider | None,
    key: str | None,
    delete_key: bool,
    settings: Settings,
) -> None:
    slot = previous.credential_slot if previous else f"{draft.provider_id}/{uuid4().hex}"
    if previous and (
        not previous.base_url or model_origin(previous.base_url) != model_origin(draft.base_url)
    ):
        if key is None and not delete_key and read_model_key(slot, settings):
            raise ValueError("更改 API 主机时，请输入该地址的新 Key 或清除当前 Key")
        # Historical snapshots keep their own credential slot when the origin changes.
        slot = f"{draft.provider_id}/{uuid4().hex}"
    assert preferences.providers is not None
    preferences.providers[draft.provider_id] = StoredProvider(
        **draft.model_dump(),
        credential_slot=slot,
        adapter=previous.adapter if previous else "generic",
    )
    if key is None and not delete_key:
        save_preferences(preferences, settings)
        return
    path = model_secret_path(settings)
    original = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    write_model_key(slot, key, settings)
    try:
        save_preferences(preferences, settings)
    except (OSError, ValueError):
        write_secret_file(path, original)
        raise
