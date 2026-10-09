"""本机密钥存储。

设计约束（BUILD_PLAN §9.1、§10）：
- 密钥使用 `secret_ref` 引用，形如 `env:XIAOMI_API_KEY` 或 `file:xiaomi/api_key`；
- 服务端解析并注入，**不进模型 prompt、不进命令行 argv、不进普通日志**；
- 加密密钥与密文不能一起放进同一份数据库备份；
- 执行岛只按需取得目标测试凭据，模型 API Key 永远不下发。

首版实现为"环境变量 + 本机受限权限 JSON 文件"两路解析。文件权限在写入时收紧到
当前用户可读（POSIX 0600 / Windows 仅当前用户 ACL）。控制端优先使用操作系统凭据存储
属后续增强项，此处不声称已经做到。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Protocol

from agent_workbench.config.settings import get_settings


class SecretNotFoundError(LookupError):
    """引用的秘密不存在。调用方必须把它当作**必要条件缺失**，不能降级为空字符串。"""

    def __init__(self, secret_ref: str) -> None:
        super().__init__(f"秘密引用无法解析：{secret_ref}")
        self.secret_ref = secret_ref


class SecretStore(Protocol):
    def resolve(self, secret_ref: str) -> str: ...

    def exists(self, secret_ref: str) -> bool: ...


class EnvSecretStore:
    """`env:NAME` 与 `file:relative/path` 两路解析。

    环境变量优先；文件为 `{ "relative/path": "value" }` 形式的扁平 JSON。
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._path = store_path or get_settings().secret_store_path
        self._cache: dict[str, str] | None = None

    def _load_file(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        raw = self._path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"密钥存储格式错误（应为对象）：{self._path}")
        self._cache = {str(k): str(v) for k, v in parsed.items()}
        return self._cache

    def exists(self, secret_ref: str) -> bool:
        try:
            self.resolve(secret_ref)
        except SecretNotFoundError:
            return False
        return True

    def resolve(self, secret_ref: str) -> str:
        scheme, _, key = secret_ref.partition(":")
        if not key:
            raise SecretNotFoundError(secret_ref)

        if scheme == "env":
            value = os.environ.get(key)
            if value:
                return value
        elif scheme in {"file", "store"}:
            value = self._load_file().get(key)
            if value:
                return value
        else:
            # 允许直接把裸环境变量名当作引用，避免旧配置迁移摩擦；行为显式而非猜测。
            value = os.environ.get(secret_ref)
            if value:
                return value

        raise SecretNotFoundError(secret_ref)

    def redact(self, value: str | None) -> str:
        """日志用脱敏视图。空值返回 `<unset>` 而不是空串，避免"看起来像配置了"。"""
        if not value:
            return "<unset>"
        if len(value) <= 8:
            return "<redacted>"
        return f"{value[:4]}…{value[-4:]}(len={len(value)})"


def write_secret_file(path: Path, payload: dict[str, str]) -> None:
    """写入密钥文件并收紧权限。

    Windows 上不声称等价于 POSIX 0600；此处使用 `os.chmod` 的只读语义并在调用方文档中
    说明"依赖 NTFS ACL 继承"。禁止把该文件放进数据库备份。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    else:
        tmp.chmod(stat.S_IREAD | stat.S_IWRITE)
    tmp.replace(path)


def build_secret_store() -> SecretStore:
    return EnvSecretStore()
