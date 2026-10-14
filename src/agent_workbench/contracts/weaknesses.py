"""弱点（Finding 升级形态）与线索—弱点关系的读写协议。

权威归属（方案 §3.4）：弱点、条件、线索、关系、用途与可推进性判断都保存在**业务表**；
本模块只定义**数据结构**与**稳定标识**，不持有状态，也不复制事实。

关键语义（方案 §3.1 / §3.2）：

- `weakness_kind` 区分"独立缺陷"与"可供后续利用的条件"，不是严重等级；
- `provided_conditions` 的 `evidence_refs` 说明**判断依据**，不自动证明陈述成立；
- 条件 key 由领域服务在幂等事务内分配并回传，工具重放返回原 key；
- `connection_review.basis_fingerprint` 覆盖"当前版本后续用途判断"的依据指纹，
  判断元数据本身**不**进入指纹，避免刚写入就使自己过期。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_workbench.contracts.enums import (
    LeadWeaknessRelationState,
    LeadWeaknessRole,
    WeaknessKind,
)

#: 单条条件陈述的字符上限（有界摘要，长正文落工件）。
MAX_CONDITION_STATEMENT_CHARS = 400
#: 单次写入的条件数量上限（上下文资源边界，不是总探索数量上限）。
MAX_CONDITIONS_PER_WRITE = 20
#: 用途判断里可引用的关联线索数量上限。
MAX_CONNECTION_LEADS = 10
#: 限制说明的字符上限。
MAX_LIMITATION_CHARS = 300


def canonical_condition_key(statement: str, *, seed: str = "") -> str:
    """由条件陈述派生**稳定** key（16 位十六进制）。

    同一陈述在同一弱点内得到同一 key，因此"同内容重复更新不增加知识进展"可以被程序检查。
    大小写与空白差异被归一化：它们不构成新的条件身份。
    `seed` 只在调用方需要区分不同命名空间时使用。
    """
    normalized = " ".join(statement.split()).casefold()
    payload = f"{seed}\x1f{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


class ProvidedCondition(BaseModel):
    """一条**有证据引用**的条件陈述。

    `evidence_refs` 说明"模型凭什么这么说"，而不是"条件已成立"。
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=MAX_CONDITION_STATEMENT_CHARS)
    evidence_refs: list[str] = Field(default_factory=list)
    limitations: str | None = Field(default=None, max_length=MAX_LIMITATION_CHARS)

    @field_validator("statement")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # 归一化空白：纯排版差异不构成新的条件内容，也不推进知识版本。
        text = " ".join(value.split())
        if not text:
            raise ValueError("条件陈述不能为空")
        return text

    @field_validator("evidence_refs")
    @classmethod
    def _dedupe_refs(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item) for item in value if str(item).strip()))


class ApplicabilitySpec(BaseModel):
    """适用资产、身份、已知前置条件与限制。

    复用已有 `asset_id` / `identity_id`，**不**建立第二份身份库。
    """

    model_config = ConfigDict(extra="forbid")

    asset_ids: list[str] = Field(default_factory=list)
    identity_ids: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @staticmethod
    def normalize(raw: dict[str, Any] | None) -> ApplicabilitySpec:
        return ApplicabilitySpec.model_validate(raw or {})


class ConnectionReview(BaseModel):
    """模型对**当前版本**后续用途的短判断。

    - `linked_lead_ids`：已产生的后续线索，必须引用真实存在的关联；
    - `no_connection_note`：暂未找到后续用途时的短说明（允许保留，但不虚构连接）；
    - `basis_fingerprint`：写入时的依据指纹，用于判断本判断是否已过期。
    """

    model_config = ConfigDict(extra="forbid")

    linked_lead_ids: list[str] = Field(default_factory=list)
    no_connection_note: str | None = Field(default=None, max_length=MAX_CONDITION_STATEMENT_CHARS)
    evidence_refs: list[str] = Field(default_factory=list)
    basis_fingerprint: str | None = Field(default=None, max_length=64)
    source_tool_call_id: str | None = Field(default=None, max_length=128)
    reviewed_content_revision: int | None = None

    def has_judgment(self) -> bool:
        return bool(self.linked_lead_ids or (self.no_connection_note or "").strip())


class WeaknessRelationRef(BaseModel):
    """工具入参里的一条关系引用。"""

    model_config = ConfigDict(extra="forbid")

    weakness_id: str = Field(min_length=1)
    role: Literal["basis", "result"]
    condition_key: str | None = Field(default=None, max_length=64)
    requirement_key: str | None = Field(default=None, max_length=64)
    #: 调用方看到的来源弱点版本；写入时与当前版本核对，过期则明确冲突。
    source_weakness_revision: int | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @property
    def role_enum(self) -> LeadWeaknessRole:
        return LeadWeaknessRole(self.role)


class RequiredConditionRef(BaseModel):
    """线索的前置条件：可指向已有弱点条件、身份/资产，或另一条线索的预期产出。"""

    model_config = ConfigDict(extra="forbid")

    condition_key: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=1, max_length=MAX_CONDITION_STATEMENT_CHARS)
    #: 已有弱点条件
    weakness_id: str | None = None
    #: 另一条线索预期获得该条件（依赖意图，**不**伪造一个已存在的弱点）
    producer_lead_id: str | None = None
    identity_id: str | None = None
    asset_id: str | None = None
    satisfied: bool = False
    #: 只有明确需要用户/环境提供的条件才允许整体等待；探索可产生的条件不能冒充外部阻塞。
    external: bool = False


class ActionabilityAssessment(BaseModel):
    """线索当前能否推进；与 `LeadStatus`（工作状态）分开。

    `basis_fingerprint` 覆盖线索假设、下一动作、条件、来源弱点的有效版本，
    以及相关身份/范围状态；**不含**判断字段本身、纯排序变化和展示时间。
    """

    model_config = ConfigDict(extra="forbid")

    state: Literal["ready", "needs_condition", "no_new_action", "needs_review"]
    note: str = Field(min_length=1, max_length=500)
    basis_fingerprint: str | None = Field(default=None, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list)
    operation_refs: list[str] = Field(default_factory=list)
    #: 尚有哪些未知、为什么当前没有能区分它们的新动作（`no_new_action` 必须说明）。
    unknowns: list[str] = Field(default_factory=list)
    source_tool_call_id: str | None = Field(default=None, max_length=128)
    revision: int = 0

    @field_validator("state")
    @classmethod
    def _valid_state(cls, value: str) -> str:
        if value not in {"ready", "needs_condition", "no_new_action", "needs_review"}:
            raise ValueError(f"actionability state 非法：{value}")
        return value


class WeaknessRelationLinkRead(BaseModel):
    """关系的只读投影（给检索与页面用）。"""

    link_id: str
    session_id: str
    lead_id: str
    finding_id: str
    role: LeadWeaknessRole
    condition_key: str | None = None
    requirement_key: str | None = None
    source_weakness_revision: int | None = None
    relation_state: LeadWeaknessRelationState
    evidence_refs: list[str] = Field(default_factory=list)
    source_operation_id: str | None = None
    revision: int


class WeaknessSummary(BaseModel):
    """弱点的有界摘要；**不**返回长原始正文。"""

    weakness_id: str
    title: str
    category: str
    status: str
    weakness_kind: WeaknessKind = WeaknessKind.UNCLASSIFIED
    revision: int
    evidence_refs: list[str] = Field(default_factory=list)
    condition_keys: list[str] = Field(default_factory=list)
    identity_id: str | None = None
    asset_id: str | None = None
    connection_reviewed: bool = False
    connection_stale: bool = False


def content_revision_payload(
    *,
    title: str,
    description: str,
    category: str,
    weakness_kind: str,
    applicability: dict[str, Any],
    provided_conditions: list[dict[str, Any]],
) -> str:
    """实质内容哈希。**只**覆盖知识内容。

    修改"已分析后续用途"的元数据（`connection_review`）**不**进入这里，
    因此写入用途判断不会立刻把该判断判为过期（方案 §3.1）。
    """
    payload = {
        "title": title,
        "description": description,
        "category": category,
        "weakness_kind": weakness_kind,
        "applicability": applicability,
        "conditions": sorted(
            (
                {
                    "key": item.get("key"),
                    "statement": item.get("statement"),
                    "limitations": item.get("limitations"),
                    "evidence_refs": sorted(item.get("evidence_refs") or []),
                }
                for item in provided_conditions
            ),
            key=lambda item: str(item.get("key")),
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def connection_basis_fingerprint(
    *,
    weakness_id: str,
    content_revision: int,
    content_hash: str,
    lead_ids: list[str],
    condition_keys: list[str],
) -> str:
    """用途判断的依据指纹。

    只包含"能让该判断失效"的实质来源版本；显式排除 `connection_review` 自身。
    """
    payload = {
        "weakness_id": weakness_id,
        "content_revision": content_revision,
        "content_hash": content_hash,
        "leads": sorted(str(item) for item in lead_ids),
        "conditions": sorted(str(item) for item in condition_keys),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def actionability_basis_fingerprint(
    *,
    hypothesis: str,
    next_action: str | None,
    required_conditions: list[dict[str, Any]],
    basis_revisions: list[tuple[str, int | None]],
    identity_state: str | None,
) -> str:
    """可推进性判断的依据指纹。

    只包含线索假设、下一动作、条件与来源弱点的有效版本，以及身份/范围状态；
    纯排序变、展示时间与判断字段本身都不进入。
    """
    payload = {
        "hypothesis": hypothesis.strip(),
        "next_action": (next_action or "").strip(),
        "conditions": sorted(
            (
                {
                    "key": item.get("condition_key"),
                    "description": item.get("description"),
                    "producer": item.get("producer_lead_id"),
                    "weakness": item.get("weakness_id"),
                    "identity": item.get("identity_id"),
                    "asset": item.get("asset_id"),
                    "external": bool(item.get("external")),
                    "satisfied": bool(item.get("satisfied")),
                }
                for item in required_conditions
            ),
            key=lambda item: str(item.get("key")),
        ),
        "basis": sorted((str(key), revision) for key, revision in basis_revisions),
        "identity": identity_state,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


__all__ = [
    "MAX_CONDITIONS_PER_WRITE",
    "MAX_CONDITION_STATEMENT_CHARS",
    "MAX_CONNECTION_LEADS",
    "MAX_LIMITATION_CHARS",
    "ActionabilityAssessment",
    "ApplicabilitySpec",
    "ConnectionReview",
    "ProvidedCondition",
    "RequiredConditionRef",
    "WeaknessRelationLinkRead",
    "WeaknessRelationRef",
    "WeaknessSummary",
    "actionability_basis_fingerprint",
    "canonical_condition_key",
    "connection_basis_fingerprint",
    "content_revision_payload",
]
