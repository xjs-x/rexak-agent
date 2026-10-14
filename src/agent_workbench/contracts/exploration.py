"""结束复查集合、关系分页与条件变化召回的只读投影协议。

设计约束（方案 §6.1 / §7）：

- **程序决定复查集合**：集合来自业务表枚举，模型的 `relevant_lead_ids` 只作为展示偏好，
  不能作为排除其他对象的依据，也不能用 `.limit(200)` 当作完整集合；
- 复查判断本身写在业务表（线索的 `actionability_assessment`），本模块只定义投影；
- 扫描进度（游标）由 checkpoint 保存，不在业务表另建可写任务清单；
- 条件变化召回按"对象 + 实质版本 + 触发原因"去重，防止每个普通请求都召回同一批内容。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 每页返回给模型的最大待判断对象数（上下文资源初值，不是总探索数量上限）。
REVIEW_PAGE_SIZE = 5
#: 关系分页上限。
RELATION_PAGE_SIZE = 20

ReviewItemKind = Literal["lead", "plan_step", "coverage_gap", "weakness", "stale_condition"]
RecallTrigger = Literal[
    "new_weakness",
    "condition_revised",
    "identity_changed",
    "mainline_blocked",
    "job_completed",
    "weakness_refuted",
]


class ReviewInventoryItem(BaseModel):
    """复查集合中的一个待判断对象。"""

    model_config = ConfigDict(extra="forbid")

    kind: ReviewItemKind
    object_id: str
    label: str = ""
    status: str | None = None
    actionability: str | None = None
    #: 是否已按当前依据版本复核过；未复核或依据已变化的对象必须重新判断。
    stale: bool = True
    #: 依据指纹。提交判断时程序会核对旧版本，在事务内绑定写入后的有效版本。
    basis_fingerprint: str | None = None
    #: 可执行（有明确下一步、条件已满足）=> 不能据此自主结束。
    actionable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class ReviewInventory(BaseModel):
    """完整复查集合的**有界页面**；`complete_scan` 表示是否已覆盖全量。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    items: list[ReviewInventoryItem] = Field(default_factory=list)
    #: 本次扫描覆盖的全部对象数量（不受分页影响）。
    total_objects: int = 0
    #: 未复核或依据已过期的对象总数（不是本页数量）。
    unreviewed_count: int = 0
    actionable_count: int = 0
    waiting_count: int = 0
    #: 本页之后的游标；为 None 表示已到末尾，最后一页必须再跑一次全量核对。
    next_cursor: str | None = None
    #: 程序判定：是否还有可推进方向（决定是否允许自主结束）。
    has_direction: bool = False
    note: str = (
        "复查集合由程序从业务表枚举，不依赖结束申请中主动列出的线索；"
        "`blocked` / `deferred` 不在自动终态集合中。"
    )


class ExploredRelationItem(BaseModel):
    """关系读模型的一跳（弱点 → 线索 → 弱点）。"""

    model_config = ConfigDict(extra="forbid")

    direction: Literal["basis", "result"]
    link_id: str
    lead_id: str
    weakness_id: str
    condition_key: str | None = None
    requirement_key: str | None = None
    relation_state: str
    source_weakness_revision: int | None = None
    revision: int = 1


class ExplorationRelationPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExploredRelationItem] = Field(default_factory=list)
    next_cursor: str | None = None
    retrieval: str = "explicit_id_or_condition_relation"


class RecallCandidate(BaseModel):
    """条件变化召回的候选对象。

    `dedupe_key` = 对象 + 实质版本 + 触发原因，用于防止同一内容每轮重复召回。
    """

    model_config = ConfigDict(extra="forbid")

    lead_id: str
    hypothesis: str
    status: str
    actionability: str | None = None
    recall_reason: str
    trigger: RecallTrigger
    dedupe_key: str
    relation_ids: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    revision: int = 1


class ConditionChange(BaseModel):
    """本轮实际发生的条件变化（增加 / 修订 / 失去依据）。"""

    model_config = ConfigDict(extra="forbid")

    weakness_id: str
    change: Literal["added", "revised", "removed", "invalidated"]
    condition_keys: list[str] = Field(default_factory=list)
    weakness_revision: int | None = None


__all__ = [
    "RELATION_PAGE_SIZE",
    "REVIEW_PAGE_SIZE",
    "ConditionChange",
    "ExplorationRelationPage",
    "ExploredRelationItem",
    "RecallCandidate",
    "RecallTrigger",
    "ReviewInventory",
    "ReviewInventoryItem",
    "ReviewItemKind",
]
