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
    #: 直接驱动的浏览器可执行文件。设了就**不**依赖 `playwright install` 的下载产物
    #: （执行岛 sudo 需要密码，装不了系统依赖，系统 Chromium 是最实际的路径）。
    browser_executable: Path | None = None
    browser_headless: bool = True
    #: 按身份持久化 storage_state 的目录；用于进程重启后恢复登录态。
    browser_state_root: Path | None = None
    #: 单次浏览器动作超时
    browser_action_timeout_ms: int = 30_000
    #: 单次截图/下载的大小上限
    browser_artifact_limit_bytes: int = 20 * 1024 * 1024

    @property
    def protocol_version(self) -> int:
        return 1

    @property
    def capabilities_version(self) -> str:
        return "1"

    def ensure_directories(self) -> None:
        for path in (self.data_root, self.workspace_root, self.data_root / "jobs"):
            path.mkdir(parents=True, exist_ok=True)
        self.browser_states.mkdir(parents=True, exist_ok=True)

    @property
    def ledger_path(self) -> Path:
        return self.data_root / "executor-ledger.sqlite3"

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"

    @property
    def browser_states(self) -> Path:
        """按身份持久化 storage_state 的位置。

        与任务工作区**分开**：浏览器状态不放进命令/文件工具能读到的目录，
        避免某个任务读到另一个身份的会话。
        """
        return self.browser_state_root or (self.data_root / "browser-states")

    def resolve_browser_executable(self) -> Path | None:
        """找到可用的浏览器可执行文件。

        优先显式配置；否则看 Playwright 自带的下载产物；最后回落到系统 Chromium。
        找不到就返回 None —— 能力预检据此如实报告 `browser_available=false`，
        **不**因为"python 包装了"就声称浏览器可用。
        """
        if self.browser_executable is not None:
            candidate = self.browser_executable.expanduser()
            return candidate if candidate.exists() else None

        if self.playwright_browsers_path is not None:
            root = self.playwright_browsers_path.expanduser()
        else:
            root = Path.home() / ".cache" / "ms-playwright"
        if root.is_dir():
            for pattern in (
                "chromium-*/chrome-linux/chrome",
                "chromium_headless_shell-*/chrome-linux/headless_shell",
            ):
                hits = sorted(root.glob(pattern))
                if hits:
                    return hits[-1]

        for candidate in (
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/google-chrome"),
        ):
            if candidate.exists():
                return candidate
        return None


@lru_cache(maxsize=1)
def get_executor_settings() -> ExecutorSettings:
    return ExecutorSettings()


def new_boot_id() -> str:
    """每次进程启动生成一次，用于拒绝 PID 复用导致的误杀（§8.4）。"""
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
