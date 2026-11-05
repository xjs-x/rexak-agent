"""Bounded collaboration data, never execution authority or verified Findings."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.contracts.delegation import WorkerOutputRef
from agent_workbench.contracts.worker_evidence import EvidenceReadRef


class FindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1000)
    background: str = Field(default="", max_length=2000)
    evidence_reads: list[EvidenceReadRef] = Field(default_factory=list, max_length=8)
    output_refs: list[WorkerOutputRef] = Field(default_factory=list, max_length=8)


class WorkerFindingRequest(FindingInput):
    task_id: str = Field(min_length=1, max_length=128)
    worker_id: UUID
    context_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=256)


class DelegatedMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=3000)
    context_refs: list[UUID] = Field(default_factory=list, max_length=8)


class WorkerMessageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    status: Literal["accepted", "queued", "consumed", "context_lost", "not_delivered", "delivery_unknown"]
