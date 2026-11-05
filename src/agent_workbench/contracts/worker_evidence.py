"""Worker 只读证据通道；身份字段由运行时填充，不是模型参数。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkerEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=128)
    worker_id: UUID
    context_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=256)
    action: Literal["list", "read"]
    artifact_id: UUID | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8192, ge=4, le=16384)
    cursor: UUID | None = None
    query: str = Field(default="", max_length=128)


class EvidenceReadRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: UUID
    artifact_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    view_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    offset: int = Field(ge=0)
    returned_bytes: int = Field(gt=0, le=16384)
    truncated: bool
