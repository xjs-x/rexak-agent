"""当前计划的只读投影；权威数据仍保存在 LangGraph checkpoint。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanStepPatch(BaseModel):
    """轻量计划局部更新；完成仅代表工作记录，不代表漏洞验证。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step_id: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["pending", "in_progress", "done", "dropped", "blocked"] | None = None
    rationale: str | None = Field(default=None, max_length=500)
    lead_id: str | None = None


class PlanStepRead(BaseModel):
    step_id: str
    description: str
    status: Literal["pending", "in_progress", "done", "dropped", "blocked"]
    rationale: str | None = None
    lead_id: str | None = None
    legacy_metadata: dict[str, object] | None = Field(
        default=None, description="旧计划附加信息，仅供历史读取，不参与执行或结束"
    )


class CurrentFocusRead(BaseModel):
    step_id: str | None = None
    lead_id: str | None = None
    intent: str
    reason: str = ""
    return_to_step_id: str | None = None
    #: 焦点来源：模型明确选择 / 由唯一进行中步骤承接。旧 checkpoint 可能缺失。
    source: Literal["explicit", "plan_inferred"] | None = None


class PlanRead(BaseModel):
    revision: int
    steps: list[PlanStepRead]
    current_focus: CurrentFocusRead | None = None
    last_plan_change: dict[str, object] | None = None
    revision_projection_error: str | None = None
    source: Literal["langgraph_checkpoint"] = "langgraph_checkpoint"
