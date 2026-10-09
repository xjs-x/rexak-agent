"""Executor 设置。

Executor **不读取**控制端的任何环境变量前缀（`WORKBENCH_`），
也**不持有**模型 API Key 或数据库凭据（§2）。
"""

from __future__ import annotations

import os
import socket
import uuid
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXECUTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    executor_id: str = Field(default_factory=lambda: socket.gethostname())
    #: 只监听执行岛回环地址（§2）
    host: str = "127.0.0.1"
    port: int = 8791

    #: 控制端令牌的校验值。为空则**拒绝启动**，避免无鉴权的管理接口。
    token: str = ""

    data_root: Path = Path("/home/kali/rexak-agent-data")
    workspace_root: Path = Path("/home/kali/rexak-agent-data/workspaces")

    #: 出站网络隔离是否为执行环境级强制（§10）。首版默认 unavailable，
    #: 因为仅靠 Python URL 校验不能构成隔离，**不得**声称已生效。
    network_isolation: str = "unavailable"

    #: 断线宽限：控制端失联超过该时长后暂停新操作（§8.7）
    disconnect_grace_seconds: int = 300

    #: 单任务输出上限
    job_output_limit_bytes: int = 10 * 1024 * 1024
    #: 命令默认超时；取消时先请求终止再升级为强制结束
    command_timeout_seconds: int = 120
    command_terminate_grace_seconds: int = 10

    browser_enabled: bool = True
    playwright_browsers_path: Path | None = None

    @property
    def protocol_version(self) -> int:
        return 1

    @property
    def capabilities_version(self) -> str:
        return "1"

    def ensure_directories(self) -> None:
        for path in (self.data_root, self.workspace_root, self.data_root / "jobs"):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def ledger_path(self) -> Path:
        return self.data_root / "executor-ledger.sqlite3"

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"


@lru_cache(maxsize=1)
def get_executor_settings() -> ExecutorSettings:
    return ExecutorSettings()


def new_boot_id() -> str:
    """每次进程启动生成一次，用于拒绝 PID 复用导致的误杀（§8.4）。"""
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
