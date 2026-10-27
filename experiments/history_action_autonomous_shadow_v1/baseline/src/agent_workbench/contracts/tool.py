"""工具契约：操作身份、统一结果、工件引用、后台任务。

统一结果契约见 BUILD_PLAN §8.1。**所有工具**先转换成 `ToolResult` 再进入模型上下文，
底层 stdout 不能直接当成功标志。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_workbench.contracts.enums import (
    ArtifactAvailability,
    ExecutionCertainty,
    JobState,
    OperationStatus,
    Sensitivity,
    ToolEffectClass,
)

TOOL_RESULT_SCHEMA_VERSION = 1


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolOperation(_Base):
    """一次**逻辑**外部操作。

    在持久化之后才允许发送到 Executor。重放沿用同一 `operation_id`（§8.5）。
    显式"再测一次"必须生成新的逻辑操作 ID。
    """

    operation_id: UUID
    run_id: UUID
    session_id: UUID
    tool_name: str
    tool_call_id: str
    #: 参数规范化后的摘要；同 ID 不同 digest 视为冲突，不重放
    request_digest: str
    effect_class: ToolEffectClass
    policy_revision: int
    scope_revision: int
    identity_id: UUID | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = None
    #: Executor 侧已知过期 generation 时拒绝新派发（§4.3）
    owner_generation: int = 0
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("时间字段必须带时区（UTC）")
        return value


class ArtifactRef(_Base):
    """工件引用。模型只收到短摘要 + 可读取的 `artifact_id`（§5.3）。"""

    artifact_id: UUID
    media_type: str
    byte_size: int
    sha256: str
    availability: ArtifactAvailability
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    #: 工件内容是否为截断视图；原件另存
    truncated: bool = False
    source_operation_id: UUID | None = None
    captured_at: datetime | None = None
    summary: str | None = None


class ToolError(_Base):
    """稳定错误结构。**不能从中文错误文本猜测类型。**"""

    code: str
    message: str
    retryable: bool = False
    recovery_hint: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(_Base):
    """统一工具结果（§8.1）。"""

    schema_version: int = TOOL_RESULT_SCHEMA_VERSION
    operation_id: UUID
    job_id: UUID | None = None
    status: OperationStatus
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error: ToolError | None = None
    truncated: bool = False
    execution_certainty: ExecutionCertainty = ExecutionCertainty.CONFIRMED
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("operation_id")
    @classmethod
    def _uuid(cls, value: UUID) -> UUID:
        return value

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.UNKNOWN,
        }

    @classmethod
    def accepted(cls, operation_id: UUID, job_id: UUID, summary: str) -> ToolResult:
        """长任务在交互等待阈值内返回 accepted，而不是占着模型调用链直到结束（§8.3）。"""
        return cls(
            operation_id=operation_id,
            job_id=job_id,
            status=OperationStatus.ACCEPTED,
            summary=summary,
        )


class JobReceipt(_Base):
    """`POST /jobs` 的幂等接收回执。"""

    job_id: UUID
    operation_id: UUID
    state: JobState
    accepted_at: datetime
    #: 同一 operation_id + 同 request_digest 复用原 job 时为 True
    deduplicated: bool = False


class JobSnapshot(_Base):
    job_id: UUID
    operation_id: UUID
    kind: str
    state: JobState
    exit_code: int | None = None
    output_cursor: str | None = None
    byte_count: int = 0
    truncated: bool = False
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    cancel_requested: bool = False
    #: PID 必须配合启动时间/boot ID，防止重启后误杀被复用的 PID（§8.4）
    pid: int | None = None
    pid_start_ticks: int | None = None
    worker_boot_id: str | None = None
    error: ToolError | None = None


class OutputPage(_Base):
    """有界增量输出。`cursor` 不透明，客户端不得解析其内部结构。"""

    job_id: UUID
    cursor: str | None
    next_cursor: str | None
    stream: str = "stdout"
    text: str = ""
    byte_count: int = 0
    eof: bool = False
    truncated: bool = False


class ExecutorCapabilities(_Base):
    """执行岛能力声明。控制端**不默认"远端一定支持"**（§8.6）。"""

    executor_id: str
    service_version: str
    protocol_version: int
    boot_id: str
    started_at: datetime
    browser_available: bool = False
    browser_version: str | None = None
    http_available: bool = True
    command_available: bool = True
    interpreter: str | None = None
    filesystem_available: bool = True
    network_isolation: str = "unavailable"
    capabilities_version: str = "1"
    #: 执行岛支持的请求体形态。旧端未声明时按"已有模式 raw/json"处理，
    #: 控制端在调用前检查；不支持时明确返回能力不足，**不悄悄降级构造**（§6.2）。
    body_modes: list[str] = Field(default_factory=lambda: ["raw", "json"])
    #: 执行子协议能力。旧端未声明这些字段 -> 新工具确定不可用，
    #: **不回退到控制端执行命令**（规格 §5.2）。
    execution_mode: str = "disabled"
    execution_runtime_versions: list[int] = Field(default_factory=list)
    #: `/capabilities.details.execution` 的结构化能力明细（含逐项原因）。
    execution: dict[str, Any] = Field(default_factory=dict)

    def execution_capable(self, name: str) -> bool:
        """某一项执行能力是否可用。未知/未声明一律**不**当作可用。"""
        if self.execution_mode != "trusted":
            return False
        if not self.execution:
            return False
        return str(self.execution.get(name, "unavailable")) == "available"

    def execution_reason(self, name: str) -> str:
        reasons = self.execution.get("unavailable_reasons") or {}
        if isinstance(reasons, dict):
            if name in reasons:
                return str(reasons[name])
            if "execution" in reasons:
                return str(reasons["execution"])
        return ""


class ExecutorHealth(_Base):
    executor_id: str
    service_version: str
    boot_id: str
    protocol_version: int
    started_at: datetime
    clock_utc: datetime
    free_bytes: int
    uptime_seconds: float
    active_jobs: int
    capabilities_version: str = "1"


class CancelReceipt(_Base):
    job_id: UUID
    request_id: UUID
    #: 是否已确认进程退出；未确认时调用方必须显示 unknown，不能显示"已全部停止"
    state: JobState
    process_confirmed_exited: bool = False
    previous_cancel_request_id: UUID | None = None


def execution_job_state(snapshot: JobSnapshot) -> str:
    """旧执行器把 HTTP 接收失败记作 failed；此时目标副作用仍未知。"""
    state = str(snapshot.state)
    error = getattr(snapshot, "error", None)
    if (
        state == "failed"
        and error
        and error.code
        in {
            "tls_certificate_error",
            "http_timeout",
            "http_connection_error",
            "http_transport_error",
        }
    ):
        return "unknown"
    return state
