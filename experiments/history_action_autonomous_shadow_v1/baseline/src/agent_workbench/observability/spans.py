"""轻量 span 记录。

必须能拆分（§14.2）：
- 用户指令到被 Runner 接收；
- 上下文组装/记忆检索；
- 模型排队、首 token、完整响应、重试等待；
- 工具准入、SSH/隧道准备、远端排队、实际执行；
- 工件同步与工具返回后处理；
- 工具结果完成到下一模型请求发起；
- 完成提议到验证/报告完成。

**不得把全部等待混为"模型慢"。** 首版不引入 OpenTelemetry 后端。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.persistence.tables import SpanRow


@dataclass(slots=True)
class Span:
    name: str
    started_at: datetime
    started_monotonic: float
    finished_at: datetime | None = None
    duration_ms: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    session_id: UUID | None = None
    run_id: UUID | None = None
    segment_id: str | None = None
    model_call_id: UUID | None = None
    operation_id: UUID | None = None
    job_id: UUID | None = None
    error: str | None = None

    def finish(self, *, error: str | None = None) -> Span:
        self.finished_at = datetime.now(UTC)
        self.duration_ms = (time.perf_counter() - self.started_monotonic) * 1000
        self.error = error
        return self


class SpanRecorder:
    """进程内收集 + 显式冲刷。

    **不做后台自动上报**：避免在持有数据库事务时等待 I/O。
    """

    def __init__(self) -> None:
        self._spans: list[Span] = []

    def start(self, name: str, **context: Any) -> Span:
        span = Span(
            name=name,
            started_at=datetime.now(UTC),
            started_monotonic=time.perf_counter(),
            **context,
        )
        self._spans.append(span)
        return span

    def record(self, span: Span, **attributes: Any) -> Span:
        span.attributes.update(attributes)
        return span

    def drain(self) -> list[Span]:
        spans = [span for span in self._spans if span.finished_at is not None]
        self._spans = [span for span in self._spans if span.finished_at is None]
        return spans

    @property
    def pending(self) -> int:
        return len(self._spans)

    async def flush(self, session: AsyncSession) -> int:
        """把已完成的 span 写入 `spans` 表。调用方负责提交。"""
        spans = self.drain()
        for span in spans:
            session.add(
                SpanRow(
                    id=uuid4(),
                    name=span.name,
                    session_id=span.session_id,
                    run_id=span.run_id,
                    segment_id=span.segment_id,
                    model_call_id=span.model_call_id,
                    operation_id=span.operation_id,
                    job_id=span.job_id,
                    started_at=span.started_at,
                    finished_at=span.finished_at,
                    duration_ms=span.duration_ms,
                    error=span.error,
                    attributes=span.attributes,
                )
            )
        if spans:
            await session.flush()
        return len(spans)


class SpanScope:
    """上下文管理器：`with SpanScope(recorder, "model.first_token", run_id=...) as sp:`"""

    def __init__(self, recorder: SpanRecorder, name: str, **context: Any) -> None:
        self._recorder = recorder
        self._name = name
        self._context = context
        self._span: Span | None = None

    def __enter__(self) -> Span:
        self._span = self._recorder.start(self._name, **self._context)
        return self._span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        assert self._span is not None
        self._span.finish(error=None if exc is None else f"{exc!r}")
        return False
