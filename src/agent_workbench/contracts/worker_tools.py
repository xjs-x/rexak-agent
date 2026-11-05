"""Internal Pi Operations callback. Model-facing schemas remain the official Pi schemas."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkerToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=128)
    worker_id: UUID
    context_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=256)
    tool: Literal["read", "write", "edit", "bash", "grep", "find"]
    phase: Literal["begin", "step", "poll", "finish", "settle"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    step: int = Field(default=0, ge=0, le=128)
    operation_id: UUID | None = None
    result: dict[str, Any] | None = None
