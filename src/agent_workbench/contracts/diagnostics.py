"""控制台只读耗时诊断契约；时间戳只表示控制端记录时间。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ModelCallSummary(BaseModel):
    model_call_id: UUID
    run_id: UUID
    phase: str
    retry_index: int
    provider_id: str
    model_name: str
    usage_status: str
    cost_status: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    cost_amount: str | None
    cost_currency: str | None
    submitted_at: datetime | None
    first_token_at: datetime | None
    completed_at: datetime | None
    error_code: str | None


class ModelCallPage(BaseModel):
    model_calls: list[ModelCallSummary]
    next_offset: int | None


class OperationSummary(BaseModel):
    operation_id: UUID
    run_id: UUID
    tool_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    job_state: str | None
    sync_status: str | None
    executor_accepted_at: datetime | None
    executor_started_at: datetime | None
    executor_finished_at: datetime | None


class OperationPage(BaseModel):
    operations: list[OperationSummary]
    next_offset: int | None


class SpanSummary(BaseModel):
    id: UUID
    run_id: UUID | None
    name: str
    started_at: datetime
    duration_ms: float | None
    error: str | None
    decision: str | None
    status: str | None


class SpanPage(BaseModel):
    spans: list[SpanSummary]
    next_offset: int | None
