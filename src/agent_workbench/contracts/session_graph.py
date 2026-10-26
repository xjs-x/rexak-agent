"""主模型维护的 Session 双图；来源合法不等于语义独立验证。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class GraphValue(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(GraphValue):
    label: str = Field(min_length=1, max_length=240)
    source_refs: list[str] = Field(default_factory=list, max_length=32)


class Relation(GraphValue):
    from_id: str = Field(description="InformationGraph Entity ID或本批Entity temp_id")
    to_id: str = Field(description="InformationGraph Entity ID或本批Entity temp_id")
    relation: str = Field(min_length=1, max_length=400)
    status: Literal["observed", "hypothesis", "refuted", "superseded"]
    source_refs: list[str] = Field(default_factory=list, max_length=32)


class AttackState(GraphValue):
    summary: str = Field(min_length=1, max_length=400)
    entity_refs: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="关联的信息图Entity；Entity不能直接作为Attempt端点",
    )


class Attempt(GraphValue):
    from_id: str = Field(description="AttackGraph AttackState ID或本批State temp_id；不能是Entity")
    to_id: str = Field(description="AttackGraph AttackState ID或本批State temp_id；允许State自环")
    action: str = Field(min_length=1, max_length=400)
    outcome: Literal["success", "negative", "blocked", "unknown"]
    source_refs: list[str] = Field(min_length=1, max_length=32)


class AddNode(GraphValue):
    op: Literal["add_node"]
    graph: Literal["information", "attack"]
    temp_id: str = Field(min_length=1, max_length=80)
    data: Entity | AttackState


class UpdateNode(GraphValue):
    op: Literal["update_node"]
    graph: Literal["information", "attack"]
    id: str
    data: Entity | AttackState


class AddEdge(GraphValue):
    op: Literal["add_edge"]
    graph: Literal["information", "attack"]
    temp_id: str = Field(min_length=1, max_length=80)
    data: Relation | Attempt


class UpdateEdge(GraphValue):
    op: Literal["update_edge"]
    graph: Literal["information", "attack"]
    id: str
    data: Relation | Attempt


GraphOperation = Annotated[AddNode | UpdateNode | AddEdge | UpdateEdge, Field(discriminator="op")]
