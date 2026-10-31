"""主模型维护的 Session 双图；来源合法不等于语义独立验证。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_SOURCE_DESCRIPTION = (
    "同一Session真实回执的operation:<UUID>或可读artifact:<UUID>；"
    "不能传job_id、tool_call_id、page_id，也不能编造来源。"
)
_NODE_GRAPH_DESCRIPTION = "information节点data只能是Entity；attack节点data只能是AttackState。"
_EDGE_GRAPH_DESCRIPTION = "information边data只能是Relation；attack边data只能是Attempt。"
_TEMP_DESCRIPTION = (
    "只在本次update_graph的operations内有效且整批唯一，可前向引用；"
    "下一次调用必须使用成功回执id_mapping的持久化ID值，不能复用临时键。"
)


class GraphValue(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(GraphValue):
    label: str = Field(
        min_length=1, max_length=240, description="信息图Entity的短标签；不是AttackState.summary。"
    )
    source_refs: list[str] = Field(
        default_factory=list, max_length=32, description=_SOURCE_DESCRIPTION
    )


class Relation(GraphValue):
    from_id: str = Field(
        description="当前信息图Entity持久化ID或本批Entity temp_id；不能引用State。"
    )
    to_id: str = Field(description="当前信息图Entity持久化ID或本批Entity temp_id；不能引用State。")
    relation: str = Field(min_length=1, max_length=400)
    status: Literal["observed", "hypothesis", "refuted", "superseded"]
    source_refs: list[str] = Field(
        default_factory=list, max_length=32, description=_SOURCE_DESCRIPTION
    )


class AttackState(GraphValue):
    summary: str = Field(
        min_length=1,
        max_length=400,
        description="攻击图State的短摘要；本对象没有label或source_refs字段。",
    )
    entity_refs: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="当前信息图Entity持久化ID或本批Entity temp_id；旧批临时ID无效。Entity不能直接作为Attempt端点。",
    )


class Attempt(GraphValue):
    from_id: str = Field(
        description="当前攻击图AttackState持久化ID或本批State temp_id；不能是Entity或旧批临时ID。"
    )
    to_id: str = Field(
        description="当前攻击图AttackState持久化ID或本批State temp_id；允许State自环。"
    )
    action: str = Field(min_length=1, max_length=400)
    outcome: Literal["success", "negative", "blocked", "unknown"]
    source_refs: list[str] = Field(min_length=1, max_length=32, description=_SOURCE_DESCRIPTION)


class AddNode(GraphValue):
    op: Literal["add_node"]
    graph: Literal["information", "attack"] = Field(description=_NODE_GRAPH_DESCRIPTION)
    temp_id: str = Field(min_length=1, max_length=80, description=_TEMP_DESCRIPTION)
    data: Entity | AttackState = Field(
        description="information={label,source_refs}；attack={summary,entity_refs}；所有语义字段都放在data内。"
    )


class UpdateNode(GraphValue):
    op: Literal["update_node"]
    graph: Literal["information", "attack"] = Field(description=_NODE_GRAPH_DESCRIPTION)
    id: str = Field(description="get_graph或先前id_mapping返回的当前同图对象持久化ID；不是临时键。")
    data: Entity | AttackState = Field(
        description="完整替换当前节点语义字段；information={label,source_refs}，attack={summary,entity_refs}。"
    )


class AddEdge(GraphValue):
    op: Literal["add_edge"]
    graph: Literal["information", "attack"] = Field(description=_EDGE_GRAPH_DESCRIPTION)
    temp_id: str = Field(min_length=1, max_length=80, description=_TEMP_DESCRIPTION)
    data: Relation | Attempt = Field(
        description="information={from_id,to_id,relation,status,source_refs}；attack={from_id,to_id,action,outcome,source_refs}。"
    )


class UpdateEdge(GraphValue):
    op: Literal["update_edge"]
    graph: Literal["information", "attack"] = Field(description=_EDGE_GRAPH_DESCRIPTION)
    id: str = Field(description="get_graph或先前id_mapping返回的当前同图边持久化ID；不是临时键。")
    data: Relation | Attempt = Field(
        description="完整替换当前边语义字段；Relation只能Entity到Entity，Attempt只能State到State。"
    )


GraphOperation = Annotated[AddNode | UpdateNode | AddEdge | UpdateEdge, Field(discriminator="op")]
