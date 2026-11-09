"""Ultra 第一阶段协议；Worker 结果是低信任观察，不是漏洞验证。"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_workbench.contracts.worker_evidence import EvidenceReadRef


class ExecutionMode(StrEnum):
    STANDARD = "standard"
    ULTRA = "ultra"


class DelegationState(StrEnum):
    INTENT = "intent"
    SUBMITTING = "submitting"
    UNKNOWN = "unknown"
    ACCEPTED = "accepted"
    WORKING = "working"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


TERMINAL_DELEGATIONS = frozenset({"completed", "failed", "cancelled", "rejected"})


class DelegationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=4000)
    resume_from_delegation_id: UUID | None = None
    context_refs: list[UUID] = Field(default_factory=list, max_length=16)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=16)
    background: str = Field(default="", max_length=8000)
    completion_requirements: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=8)

    @field_validator("objective")
    @classmethod
    def nonblank_objective(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("任务目标不能为空白")
        return value

    @field_validator("completion_requirements")
    @classmethod
    def nonblank_requirements(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("完成要求不能为空白")
        return values


class WorkerUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(max_length=64)
    model: str = Field(max_length=128)
    calls: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    usage_status: Literal["reported", "unknown", "mock"]
    # Old saved Pi results reported uncached input; never rewrite those artifacts.
    input_token_basis: Literal["uncached", "total_including_cache"] = "uncached"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    failure_reason: str | None = Field(default=None, max_length=128)


class ContextSource(BaseModel):
    """数据库来源快照，仅元数据，不代表已阅读或独立验证。"""

    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=0)
    media_type: str = Field(max_length=128)
    captured_at: str = Field(max_length=64)
    source_operation_id: UUID | None


class WorkerTaskPackage(DelegationInput):
    """Python 校验并持久化后才能发送的版本化交接包。身份不是模型参数。"""

    handoff_version: Literal[1] = 1
    context_id: UUID
    session_id: UUID | None = None
    context_sources: list[ContextSource] = Field(max_length=16)
    evidence_access: Literal["session_read_v1"] | None = None
    execution_access: Literal["pi_tools_v1"] | None = None
    collaboration_access: Literal["collaboration_v1"] | None = None
    input_files: list["WorkerInputFile"] = Field(default_factory=list, max_length=16)


class WorkerInputFile(BaseModel):
    """Worker-visible metadata, excluding Executor-only receipt fields."""

    model_config = ConfigDict(extra="forbid")
    attachment_id: UUID
    filename: str = Field(max_length=240)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=0, le=67108864)
    worker_workspace_id: UUID
    relative_path: str
    session_id: UUID
    operation_id: UUID
    source: str


class WorkerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["model_description", "mock_tool"]
    text: str = Field(max_length=2000)


class WorkerOutputRef(BaseModel):
    """Controller-issued reference to bytes actually captured from this Worker's execution."""

    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    resource_id: UUID
    artifact_id: UUID
    stream: Literal["stdout", "stderr"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stored_offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    observed_size: int = Field(ge=0)
    source_bytes: int = Field(ge=0)
    missing_ranges: list[list[int]] = Field(default_factory=list, max_length=64)
    missing_ranges_truncated: bool = False
    truncated: bool
    decode_status: Literal["utf8", "boundary_trimmed", "invalid_utf8"]


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=128)
    status: Literal["completed", "failed", "cancelled"]
    summary: str = Field(max_length=2000)
    observations: list[WorkerObservation] = Field(max_length=32)
    evidence_refs: list[str] = Field(max_length=16)
    evidence_reads: list[EvidenceReadRef] = Field(default_factory=list, max_length=16)
    output_refs: list[WorkerOutputRef] = Field(default_factory=list, max_length=32)
    output_refs_truncated: bool = False
    unresolved_questions: list[str] = Field(max_length=16)
    suggested_next_steps: list[str] = Field(max_length=16)
    context_id: UUID | None = None
    model_usage: WorkerUsage | None = None




class DelegationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    parent_session_id: UUID
    parent_run_id: UUID
    parent_tool_call_id: str
    worker_id: str | None
    a2a_task_id: str | None
    context_id: UUID | None = None
    objective: str
    state: DelegationState
    remote_state: str | None
    summary: str
    result_artifact_id: UUID | None
    diagnostic: str | None
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime


class DelegationList(BaseModel):
    tasks: list[DelegationRead]
    runtime: Literal["pi"] = "pi"
    note: str = "新子任务可使用独立 Kali 工作区的六个基础工具及历史证据读取；不支持 Pi 断点续跑。"
