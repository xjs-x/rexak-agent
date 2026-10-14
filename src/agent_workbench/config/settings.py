"""控制端进程设置。

全部字段可通过 `WORKBENCH_` 前缀的环境变量覆盖。
本模块只读取环境与版本化默认值；数据库层用户设置由上层合并覆盖。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """控制端设置。Runner 与 API 共用同一份设置。"""

    model_config = SettingsConfigDict(
        env_prefix="WORKBENCH_",
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 数据库 ----------
    database_url: str = "postgresql+psycopg://workbench:workbench@127.0.0.1:55432/workbench"
    checkpoint_schema: str = "agent_checkpoint"
    memory_schema: str = "agent_memory"
    app_schema: str = "app"
    db_pool_min: int = 4
    db_pool_max: int = 8
    db_connect_timeout_seconds: int = Field(default=10, gt=0)

    # ---------- 控制端服务 ----------
    api_host: str = "127.0.0.1"
    api_port: int = 8770
    session_secret: str = "change-me-local-session-secret"
    access_token: str = "change-me-local-access-token"
    allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    owner_id: str = "local-owner"
    # 项目事实与 owner 级方法经验同时隔离；None 保留日常命名空间。
    memory_read_branch: str | None = None
    memory_write_branch: str | None = None
    experiment_trial_id: str | None = None
    experiment_group: str | None = None

    # ---------- 数据根目录 ----------
    data_root: Path = REPO_ROOT / "data"
    remote_data_root: str = "/home/kali/rexak-agent-data"

    # ---------- 模型 ----------
    default_provider: str = "xiaomi"
    secret_store_path: Path = Path("~/.workbench/secrets.json")

    # ---------- 上下文与并发（可调资源初值）----------
    context_trigger_ratio: float = 0.6
    output_reserve_tokens: int = 8192
    #: 恢复尝试时可用的更大输出额度。**默认不提高**（None 表示沿用请求额度）。
    #: 8,192 -> 16,384 只是一个待实验的配置组合，不是任何供应商的默认或最佳值。
    recovery_output_limit_tokens: int | None = Field(default=None, gt=0)
    #: 单次逻辑模型决策允许的自动恢复次数（初次请求之外），不是任务尝试总数限制。
    max_response_recoveries: int = Field(default=2, ge=0, le=8)
    tool_max_concurrent: int = 4
    origin_max_concurrent: int = 2
    browser_write_max_concurrent: int = 1

    # ---------- Executor ----------
    executor_id: str = "kali-01"
    executor_host: str = "192.168.72.134"
    executor_port: int = 22
    executor_user: str = "kali"
    executor_ssh_key: Path = Path("~/.xak/id_ed25519")
    executor_known_hosts: Path = Path("~/.xak/known_hosts")
    executor_remote_port: int = 8791
    executor_local_port: int = 0
    executor_token: str = "change-me-executor-token"
    executor_grace_seconds: int = 300

    # ---------- 任务生命周期 ----------
    tool_interactive_threshold_seconds: int = 20
    single_operation_timeout_seconds: int = 120
    #: 连续无有效流式分片的等待上限；思考分片也算有效活动。
    model_request_timeout_seconds: int = Field(default=300, gt=0)
    #: 单次模型调用的墙钟保护，不是任务总时长限制。
    model_request_max_seconds: int = Field(default=1800, gt=0)
    run_lease_seconds: int = 60
    lease_heartbeat_seconds: int = 15
    segment_step_threshold: int = 40
    segment_decision_rounds: int = Field(default=4, ge=1, le=5)
    #: 后台任务唤醒巡检间隔。等待期间**不调用模型**，只按这个间隔核对远端状态。
    #: 这是资源初值，不是任务总时长限制。
    job_wakeup_interval_seconds: float = 3.0
    runner_max_concurrent_runs: int = Field(default=2, ge=1, le=16)

    # ---------- 保留与上限 ----------
    event_retention_days: int = 30
    inline_output_limit_bytes: int = 65536
    job_output_limit_bytes: int = 10 * 1024 * 1024
    backup_path: Path = REPO_ROOT / "backups"

    # ---------- 供应商凭据（secret_ref 解析来源之一）----------
    xiaomi_api_key: str | None = Field(default=None, alias="XIAOMI_API_KEY")
    xiaomi_base_url: str = Field(default="https://api.xiaomimimo.com/v1", alias="XIAOMI_BASE_URL")
    xiaomi_model: str = Field(default="mimo-v2.6-pro", alias="XIAOMI_MODEL")

    @field_validator(
        "data_root",
        "backup_path",
        "secret_store_path",
        "executor_ssh_key",
        "executor_known_hosts",
        mode="after",
    )
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return Path(str(value)).expanduser()

    @property
    def allowed_origin_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    # ---------- 派生路径 ----------
    @property
    def projects_root(self) -> Path:
        """按 项目/任务/工件 组织的控制端数据根。"""
        return self.data_root / "projects"

    @property
    def artifact_root(self) -> Path:
        return self.data_root / "artifacts"

    @property
    def tmp_root(self) -> Path:
        return self.data_root / "tmp"

    @property
    def log_root(self) -> Path:
        return self.data_root / "logs"

    @property
    def run_root(self) -> Path:
        return self.data_root / "runs"

    def ensure_directories(self) -> None:
        for path in (
            self.data_root,
            self.projects_root,
            self.artifact_root,
            self.tmp_root,
            self.log_root,
            self.run_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中可用 `get_settings.cache_clear()` 重置。"""
    return Settings()
