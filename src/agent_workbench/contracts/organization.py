"""聊天与项目侧栏协议。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NameUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=256)


class SessionListItem(BaseModel):
    id: UUID
    project_id: UUID | None
    title: str
    status: str
    provider_id: str | None
    model_name: str | None
    latest_run_status: str | None = None
    created_at: datetime


class SessionList(BaseModel):
    sessions: list[SessionListItem]
    next_offset: int | None
