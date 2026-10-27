"""聊天中的工具操作展示契约。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ToolActivitySummary(BaseModel):
    run_id: UUID
    tool_call_id: str
    tool_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    operation_id: UUID | None
    job_id: UUID | None
    job_state: str | None
    sync_status: str | None


class ToolActivityPage(BaseModel):
    activities: list[ToolActivitySummary]
    next_offset: int | None


class ToolActivityDetail(BaseModel):
    request_text: str
    result_text: str
    note: str
    execution_context: dict[str, Any] | None = None
