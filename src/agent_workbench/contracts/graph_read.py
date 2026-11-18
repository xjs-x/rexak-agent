"""Shared read contracts; identity is supplied by the controller, never the model."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph: Literal["information", "attack", "both"] = "both"
    kind: Literal["nodes", "edges", "both"] = "both"
    ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list, max_length=64)
    keyword: str = Field(default="", max_length=240)
    status: Literal["observed", "hypothesis", "refuted", "superseded"] | None = None
    outcome: Literal["success", "negative", "blocked", "unknown"] | None = None
    expand: bool = Field(default=False, description="从 ids 沿真实边展开；返回种子、边和端点，不再按 kind 裁掉邻居。status/outcome 约束所遍历的同类边。")
    direction: Literal["in", "out", "both"] = "both"
    hops: int = Field(default=1, ge=1, le=4)
    cross_graph: bool = Field(default=False, description="沿 AttackState.entity_refs 双向展开；graph 约束种子所在图，关联结果可跨图。每次真实引用或图边各计一跳。")
    limit: int = Field(default=40, ge=1, le=200)
    token_budget: int = Field(default=4096, ge=256, le=8192)
    expected_revision: int | None = Field(default=None, ge=0)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def check_expansion(self):
        if (self.expand or self.cross_graph) and not self.ids:
            raise ValueError("Expansion requires explicit seed IDs")
        return self


class GraphRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["overview", "query", "read"] = "overview"
    query: GraphQuery = Field(default_factory=GraphQuery)
    token_budget: int = Field(default=4096, ge=0, le=8192)
    category_limit: int = Field(default=4, ge=0, le=20)


class WorkerGraphRequest(GraphRead):
    task_id: str = Field(min_length=1, max_length=128)
    worker_id: UUID
    context_id: UUID
