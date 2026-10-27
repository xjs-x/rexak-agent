"""附件协议；正文仅在有界读取结果中出现。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class AttachmentRead(BaseModel):
    attachment_id: UUID
    session_id: UUID
    source_message_id: UUID | None
    filename: str
    content_type: str
    size: int
    sha256: str
    status: str
    parse_status: str
    readable: bool
    line_count: int | None = None
    page_count: int | None = None
    source: str = "user_upload"
    created_at: datetime
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttachmentPage(BaseModel):
    attachments: list[AttachmentRead]
    next_offset: int | None = None


class AttachmentSlice(BaseModel):
    attachment_id: UUID
    source: str = "user_upload_untrusted"
    reference: str
    status: str
    text: str = ""
    page: int = 1
    start_line: int = 1
    end_line: int = 1
    char_offset: int = 0
    next_cursor: dict[str, int] | None = None
    note: str
