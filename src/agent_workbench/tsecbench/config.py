"""评测适配层配置：只读环境与私有文件，**不把令牌放进仓库或日志**。

令牌与 VPN 材料存放在仓库之外（默认 ``~/.workbench/tsecbench/``）：
- ``tsecbench.env``：``BENCHMARK_TOKEN`` / ``BENCHMARK_BASE_URL``；
- ``client.ovpn``：平台下发的 VPN 配置（含私钥，绝不入库）。

读取优先级：进程环境变量 > 私有 env 文件 > 默认值。这样 CI 或临时覆盖不需要改文件。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.config.settings import get_settings

#: 私有配置目录（仓库之外）；可用 ``WORKBENCH_TSECBENCH_HOME`` 覆盖。
PRIVATE_HOME = Path("~/.workbench/tsecbench")

DEFAULT_ENV_FILE_NAME = "tsecbench.env"
DEFAULT_OVPN_NAME = "client.ovpn"


class ConfigError(RuntimeError):
    """配置缺失或不可用；消息面向操作者，不含任何秘密值。"""


class BenchmarkConfig(BaseModel):
    """评测参数快照。

    ``token`` / ``api_token`` 标记为 ``repr=False, exclude=True``：
    任何 ``model_dump`` 或日志格式化都不会带出真实值。
    """

    model_config = ConfigDict(extra="ignore")

    token: str | None = Field(default=None, repr=False, exclude=True)
    base_url: str | None = None
    api_base_url: str = "http://127.0.0.1:8770"
    api_token: str = Field(default="", repr=False, exclude=True)

    #: 单题墙钟上限（秒）。这是评测入口的保护，不是 Agent 内部的任务总时长限制。
    max_seconds: float = 3600.0
    #: 单题模型调用上限；``None`` 表示不额外限制。
    max_model_calls: int | None = None
    #: 单题工具操作上限；``None`` 表示不额外限制。
    max_tool_jobs: int | None = None
    poll_interval: float = 10.0
    cancel_grace: float = 120.0
    api_timeout: float = 30.0

    env_file: str | None = None
    ovpn_path: str | None = None

    @property
    def token_present(self) -> bool:
        return bool(self.token)

    @property
    def token_fingerprint(self) -> str:
        """令牌指纹（前 8 位 sha256）。用于核对"用的是哪一份令牌"，不可逆。"""
        if not self.token:
            return ""
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:8]

    def describe(self) -> dict[str, Any]:
        """可安全打印的摘要。"""
        return {
            "base_url": self.base_url,
            "token_present": self.token_present,
            "token_fingerprint": self.token_fingerprint,
            "api_base_url": self.api_base_url,
            "api_token_present": bool(self.api_token) and not self.api_token.startswith("change-me"),
            "env_file": self.env_file,
            "ovpn_path": self.ovpn_path,
            "max_seconds": self.max_seconds,
            "max_model_calls": self.max_model_calls,
            "max_tool_jobs": self.max_tool_jobs,
        }

    def require_platform_ready(self) -> None:
        """平台调用前的硬性前提（不涉及本机 API）。"""
        missing: list[str] = []
        if not self.token:
            missing.append("BENCHMARK_TOKEN")
        if not self.base_url:
            missing.append("BENCHMARK_BASE_URL")
        if missing:
            raise ConfigError(
                "缺少平台配置：" + "、".join(missing) + "。请写入私有文件 "
                f"{self.env_file or private_env_file()}（仓库之外，权限 600），"
                "或在当前 shell 里设置同名环境变量。"
            )

    def require_ready(self) -> None:
        """执行单题前的全部前提：平台配置 + 本机 API 访问令牌。"""
        self.require_platform_ready()
        if not self.api_token or self.api_token.startswith("change-me"):
            raise ConfigError(
                "本机访问令牌未配置：WORKBENCH_ACCESS_TOKEN 仍是默认值。"
                "评测适配器需要通过它调用本机 API（不共享 Cookie/CSRF）。"
            )


def private_home() -> Path:
    raw = os.environ.get("WORKBENCH_TSECBENCH_HOME")
    return Path(raw).expanduser() if raw else PRIVATE_HOME.expanduser()


def private_env_file() -> Path:
    return private_home() / DEFAULT_ENV_FILE_NAME


def default_ovpn_path() -> Path:
    return private_home() / DEFAULT_OVPN_NAME


def _read_private_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        raw = dotenv_values(path, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取私有配置文件 {path}：{type(exc).__name__}") from exc
    return {key: value for key, value in raw.items() if value is not None and value != ""}


def _pick(name: str, file_values: dict[str, str], default: str | None = None) -> str | None:
    from_env = os.environ.get(name)
    if from_env:
        return from_env.strip()
    value = file_values.get(name)
    if value is not None:
        return value.strip()
    return default


def _pick_number(
    name: str, file_values: dict[str, str], default: float | None
) -> float | None:
    raw = _pick(name, file_values)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，收到 {raw!r}") from exc


def _pick_int(name: str, file_values: dict[str, str]) -> int | None:
    value = _pick_number(name, file_values, None)
    return None if value is None else int(value)


def load_config(
    *,
    env_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> BenchmarkConfig:
    """加载配置。``overrides`` 由 CLI 参数提供，优先级最高。"""
    path = Path(env_file).expanduser() if env_file else private_env_file()
    file_values = _read_private_env(path)
    workbench = get_settings()

    base_url = _pick("BENCHMARK_BASE_URL", file_values)
    if base_url:
        base_url = base_url.rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("BENCHMARK_BASE_URL 必须以 http:// 或 https:// 开头")

    api_base_url = (
        _pick("WORKBENCH_API_URL", file_values)
        or f"http://{workbench.api_host}:{workbench.api_port}"
    ).rstrip("/")

    values: dict[str, Any] = {
        "token": _pick("BENCHMARK_TOKEN", file_values),
        "base_url": base_url,
        "api_base_url": api_base_url,
        "api_token": workbench.access_token,
        "max_seconds": _pick_number("TSECBENCH_MAX_SECONDS", file_values, 3600.0),
        "max_model_calls": _pick_int("TSECBENCH_MAX_MODEL_CALLS", file_values),
        "max_tool_jobs": _pick_int("TSECBENCH_MAX_TOOL_JOBS", file_values),
        "poll_interval": _pick_number("TSECBENCH_POLL_INTERVAL", file_values, 10.0),
        "cancel_grace": _pick_number("TSECBENCH_CANCEL_GRACE", file_values, 120.0),
        "api_timeout": _pick_number("TSECBENCH_API_TIMEOUT", file_values, 30.0),
        "env_file": str(path),
        "ovpn_path": str(
            Path(_pick("WORKBENCH_TSECBENCH_OVPN", file_values) or default_ovpn_path())
            .expanduser()
        ),
    }
    if overrides:
        values.update({key: value for key, value in overrides.items() if value is not None})

    config = BenchmarkConfig(**values)
    if config.max_seconds <= 0:
        raise ConfigError("max_seconds 必须大于 0")
    if config.poll_interval <= 0:
        raise ConfigError("poll_interval 必须大于 0")
    if not config.api_base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        raise ConfigError(
            "本机 API 只应通过回环地址访问；请把 WORKBENCH_API_URL 设为 "
            "http://127.0.0.1:<port>。"
        )
    return config


__all__ = [
    "DEFAULT_ENV_FILE_NAME",
    "DEFAULT_OVPN_NAME",
    "PRIVATE_HOME",
    "BenchmarkConfig",
    "ConfigError",
    "default_ovpn_path",
    "load_config",
    "private_env_file",
    "private_home",
]
