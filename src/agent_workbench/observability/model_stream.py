"""用消息 ID 关联图流分片与物理模型调用的首个可见输出。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

_MAX_PENDING = 256


class ModelStreamTracker:
    """运行期观测缓存；不是 checkpoint、业务事实或模型记忆。"""

    def __init__(self) -> None:
        self._first: dict[str, datetime] = {}
        self.active_call_id: str | None = None
        self.activity_phase = "waiting"
        self.active_purpose = "agent"
        self._auxiliary_messages: dict[str, None] = {}
        self._on_activity: Callable[[], None] | None = None

    @contextmanager
    def track_call(
        self, call_id: str, on_activity: Callable[[], None], *, purpose: str = "agent"
    ) -> Iterator[None]:
        if self.active_call_id is not None:
            raise RuntimeError("同一运行上下文不能同时观测两个模型调用")
        self.active_call_id = call_id
        self.active_purpose = purpose
        self.activity_phase = "waiting"
        self._on_activity = on_activity
        try:
            yield
        finally:
            self.active_call_id = None
            self.active_purpose = "agent"
            self._on_activity = None

    def observe(self, chunk: Any, *, observed_at: datetime | None = None) -> None:
        if not isinstance(chunk, tuple) or len(chunk) != 2:
            return
        message, metadata = chunk
        if (
            not isinstance(message, AIMessageChunk)
            or not isinstance(metadata, dict)
            or metadata.get("langgraph_node") != "model"
            or not message.id
        ):
            return
        visible = _has_visible_output(message)
        if self.active_purpose == "graph_review":
            self.mark_auxiliary(message)
        reasoning = message.additional_kwargs.get("reasoning_content")
        thinking = isinstance(reasoning, str) and bool(reasoning)
        # 空分片、心跳和其它图节点不能延长模型等待；正文首 token 仍不含思考。
        if (visible or thinking) and self._on_activity is not None:
            self.activity_phase = "responding" if visible else "thinking"
            self._on_activity()
        if not visible:
            return
        self._first.setdefault(message.id, observed_at or datetime.now(UTC))
        if len(self._first) > _MAX_PENDING:
            self._first.pop(next(iter(self._first)))

    def mark_auxiliary(self, message: Any) -> None:
        if message.id:
            self._auxiliary_messages[str(message.id)] = None
            if len(self._auxiliary_messages) > _MAX_PENDING:
                self._auxiliary_messages.pop(next(iter(self._auxiliary_messages)))

    def is_auxiliary(self, message: Any) -> bool:
        return (
            self.active_purpose == "graph_review"
            or str(getattr(message, "id", "")) in self._auxiliary_messages
        )

    def take_for_response(
        self,
        messages: Any,
        *,
        submitted_at: datetime,
        completed_at: datetime,
    ) -> datetime | None:
        if not messages:
            return None
        matches: list[datetime] = []
        for message in messages:
            if not isinstance(message, AIMessage) or not message.id:
                continue
            recorded = self._first.pop(message.id, None)
            if recorded is not None and submitted_at <= recorded <= completed_at:
                matches.append(recorded)
        return min(matches) if matches else None


def _has_visible_output(message: AIMessageChunk) -> bool:
    content = message.content
    if isinstance(content, str) and content:
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block:
                return True
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"]
            ):
                return True
    return any(
        chunk.get("name") or chunk.get("args") or chunk.get("id")
        for chunk in message.tool_call_chunks
    )
