"""当前计划的只读投影；权威数据仍保存在 LangGraph checkpoint。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlanStepRead(BaseModel):
    step_id: str
    description: str
    status: Literal["pending", "in_progress", "done", "dropped", "blocked"]
    rationale: str | None = None
    lead_id: str | None = None
    done_when: str | None = None
    resume_note: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class CurrentFocusRead(BaseModel):
    step_id: str | None = None
    lead_id: str | None = None
    intent: str
    reason: str
    return_to_step_id: str | None = None


class PlanRead(BaseModel):
    revision: int
    steps: list[PlanStepRead]
    current_focus: CurrentFocusRead | None = None
    source: Literal["langgraph_checkpoint"] = "langgraph_checkpoint"
