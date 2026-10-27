"""只读对话归档协议；正文与 Agent 上下文相互独立。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from agent_workbench.contracts.attachments import AttachmentRead


class ConversationEntry(BaseModel):
    attachments: list[AttachmentRead] = Field(default_factory=list)
    id: UUID
    role: str
    source: str
    run_id: UUID | None
    occurred_at: datetime
    preview: str
    has_more: bool
    available: bool
    response_outcome: str | None = None


class ConversationPage(BaseModel):
    entries: list[ConversationEntry]
    next_cursor: str | None


class ConversationBody(BaseModel):
    id: UUID
    role: str
    text: str
    offset: int
    next_offset: int | None
