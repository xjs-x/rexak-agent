"""SSE 事件协议（BUILD_PLAN §12.2）。

一致性要求：
- 状态变更与对应持久事件**同一业务事务**提交，提交后才广播；
- 每个 session 的事件顺序可安全续读：短事务内锁 session 事件计数分配序号；
- 客户端以 `event_id` / `session_seq` 去重，按 `session_seq` 排序，
  **不依赖跨机器时钟**；
- 游标过期时返回明确 resync 要求，客户端重新加载快照。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EVENT_SCHEMA_VERSION = 1


class EventType(StrEnum):
    RUN_STATUS = "run.status"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    PLAN_UPDATED = "plan.updated"
    # V1 审计事件预留；相应阶段实现前不产生事件，不代表能力已经启用。
    FOCUS_UPDATED = "focus.updated"
    LEADS_RECALLED = "leads.recalled"
    CONTEXT_SELECTED = "context.selected"
    SEGMENT_YIELDED = "segment.yielded"
    SNAPSHOT_CAPTURED = "snapshot.captured"
    TOOL_ACCEPTED = "tool.accepted"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    LEAD_UPDATED = "lead.updated"
    FINDING_UPDATED = "finding.updated"
    COVERAGE_UPDATED = "coverage.updated"
    USAGE_UPDATED = "usage.updated"
    MEMORY_COMPACTED = "memory.compacted"
    INPUT_REQUIRED = "input.required"
    REPORT_READY = "report.ready"
    ERROR = "error"
    STREAM_RESYNC_REQUIRED = "stream.resync_required"
    HEARTBEAT = "stream.heartbeat"


class EventEnvelope(BaseModel):
    """SSE 事件信封。同时作为 OpenAPI 可导出的 schema。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: UUID
    #: 每 session 单调递增；**同一业务事务内分配并提交**
    session_seq: int
    session_id: UUID
    run_id: UUID | None = None
    type: EventType
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def sse_id(self) -> str:
        return str(self.event_id)

    @property
    def sse_event(self) -> str:
        return str(self.type)

    def to_sse(self) -> str:
        """渲染为 SSE 帧。

        - `id:` 使用 event_id，供 `Last-Event-ID` 补读与去重；
        - `event:` 使用事件类型，前端按类型分发。
        """
        body = self.model_dump_json()
        return f"id: {self.sse_id}\nevent: {self.sse_event}\ndata: {body}\n\n"


class EventPage(BaseModel):
    """有限历史事件页；与 SSE 共用 EventEnvelope 契约。"""

    events: list[EventEnvelope]
    next_before: int | None = None


def heartbeat_frame() -> str:
    """心跳：防代理缓冲 + 让客户端知道连接仍然活着。"""
    return ": keep-alive\n\n"
