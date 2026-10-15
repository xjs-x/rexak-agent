"""Web 模型偏好与本机密钥；每次读取磁盘，使 API 与 Runner 无需重启即可共享配置。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from agent_workbench.config.secrets import write_secret_file
from agent_workbench.config.settings import Settings, get_settings

KEY_ENV = {"xiaomi": "XIAOMI_API_KEY", "bailian": "DASHSCOPE_API_KEY"}
configuration_lock = Lock()


class ModelPreferences(BaseModel):
    default_provider: str = "xiaomi"
    default_model: str = "mimo-v2.6-pro"
    endpoints: dict[str, str] = Field(default_factory=dict)


def preferences_path(settings: Settings) -> Path:
    return settings.data_root / "settings" / "models.json"


def read_preferences(settings: Settings | None = None) -> ModelPreferences:
    cfg = settings or get_settings()
    path = preferences_path(cfg)
    if path.exists():
        return ModelPreferences.model_validate_json(path.read_text(encoding="utf-8"))
    return ModelPreferences(
        default_provider=cfg.default_provider,
        default_model=cfg.model_for_provider(),
    )


def validate_endpoint(provider: str, endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    url = urlsplit(endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or "{" in endpoint
        or "}" in endpoint
        or (provider == "bailian" and not url.path.endswith("/compatible-mode/v1"))
    ):
        raise ValueError("请填写供应商的 HTTPS OpenAI 兼容 Base URL，不能包含凭据或查询参数")
    return endpoint


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
    return (
        os.environ.get(KEY_ENV[provider])
        or (cfg.xiaomi_api_key if provider == "xiaomi" else cfg.dashscope_api_key)
    ) or None


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
    write_secret_file(path, preferences.model_dump(mode="json"))
