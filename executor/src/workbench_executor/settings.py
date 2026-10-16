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

    # ---------- 执行底座（命令/脚本/PTY/监视）----------
    #: 执行模式。发行缺省 disabled；个人部署显式设置 EXECUTOR_EXECUTION_MODE=trusted。
    #: 命令可用还需解释器/平台/初始化预检成功，不能只看配置文字。
    execution_mode: str = "disabled"
    #: shell 文本使用的固定 shell（非登录模式执行：`-c` 不读 profile）。
    shell_path: str = "/bin/bash"
    #: 脚本默认解释器（python 语言）。留空则从 PATH 解析 python3。
    python_interpreter: str = ""
    #: 单资源采集日志上限（stdout/stderr 合计）。
    resource_log_limit_bytes: int = 64 * 1024 * 1024
    #: session 采集日志预算；受理时以事务预留，不能重复释放。
    session_log_budget_bytes: int = 512 * 1024 * 1024
    #: 单次受理至少需要预留的输出额度（低于此值拒绝新建）。
    min_output_reservation_bytes: int = 1024 * 1024
    #: 并行资源配额：session 总量 / 其中 PTY / 执行端全局。
    session_max_resources: int = 16
    session_max_pty: int = 8
    global_max_resources: int = 64
    #: 磁盘低水位：剩余空间低于该值时拒绝新建并告警。
    disk_low_watermark_bytes: int = 1024 * 1024 * 1024
    #: PTY 默认 quiet / startup grace。
    pty_quiet_ms: int = 8000
    pty_startup_grace_ms: int = 15000
    #: 工作区存储执行方式。没有文件系统级 quota 时必须如实标注 accounting_only。
    workspace_storage_enforcement: str = "accounting_only"
    #: 环境发现使用的程序名单（只按名称从 PATH 定位，不扫描整机）。
    required_tools: list[str] = Field(
        default_factory=lambda: ["bash", "grep", "find", "sed", "curl"]
    )
    optional_interpreters: list[str] = Field(default_factory=lambda: ["node", "python3", "perl"])

    @property
    def protocol_version(self) -> int:
        return 1

    @property
    def capabilities_version(self) -> str:
        # v2：新增执行底座能力明细（execution_mode / pty / durable_events ...）。
        return "2"

    @property
    def execution_enabled(self) -> bool:
        """命令组合法放行的**必要条件**（不是充分条件）：显式 trusted + 平台预检。"""
        if self.execution_mode != "trusted":
            return False
        return Path(self.shell_path).exists()

    @property
    def resolved_python_interpreter(self) -> str:
        if self.python_interpreter:
            return self.python_interpreter
        import shutil as _shutil

        return _shutil.which("python3") or "/usr/bin/python3"

    def ensure_directories(self) -> None:
        for path in (
            self.data_root,
            self.workspace_root,
            self.data_root / "jobs",
            self.terminal_log_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.browser_states.mkdir(parents=True, exist_ok=True)

    @property
    def ledger_path(self) -> Path:
        return self.data_root / "executor-ledger.sqlite3"

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"

    @property
    def terminal_log_root(self) -> Path:
        return self.data_root / "terminals"

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
