"""业务表定义（`app` schema，由 Alembic 管理）。

设计约束（BUILD_PLAN §5.2）：
- 共同字段：UUID 主键、带时区的 `created_at/updated_at`、必要的 `revision`、来源 ID；
- 时间存 UTC，UI 按 Asia/Shanghai 显示；
- 影响唯一性、恢复和高频查询的字段必须有明确列和索引；
- **不增加**"万能状态表"；
- 框架表（`agent_checkpoint` / `agent_memory`）不由 Alembic 管理。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent_workbench.config.settings import get_settings

APP_SCHEMA = get_settings().app_schema

#: 与 enums.ACTIVE_RUN_STATUSES 保持一致；用于"同一 session 同时最多一个活动 run"的部分唯一索引
_ACTIVE_RUN_STATUS_LITERALS = (
    "queued",
    "running",
    "waiting_tool",
    "waiting_user",
    "pausing",
    "paused",
    "cancelling",
    "recovering",
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

APP_METADATA = MetaData(schema=APP_SCHEMA, naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = APP_METADATA


class SkillInstallationRow(Base):
    __tablename__ = "skill_installations"
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    project_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    default_for_projects: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    __table_args__ = (UniqueConstraint("owner_id", "source", name="uq_skills_owner_source"),)


class SkillVersionRow(Base):
    __tablename__ = "skill_versions"
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{APP_SCHEMA}.skill_installations.id"), nullable=False
    )
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(f"{APP_SCHEMA}.artifacts.id"), nullable=False
    )
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("installation_id", "package_hash", name="uq_skill_version_content"),)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _active_run_predicate() -> Any:
    """活跃 run 部分唯一索引的 WHERE 子句。

    用文本表达式，避免在声明期引用尚未建成的 Table 对象。
    """
    literals = ", ".join(f"'{item}'" for item in _ACTIVE_RUN_STATUS_LITERALS)
    return text(f"status IN ({literals})")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PriceCatalogVersionRow(Base):
    """不可变价格版本；模型调用按版本引用，旧费用不会被新配置改写。"""

    __tablename__ = "price_catalog_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prices: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "uq_price_catalog_current",
            "is_current",
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
    )


# --------------------------------------------------------------------------- #
# 项目 / 会话 / 运行
# --------------------------------------------------------------------------- #


class ProjectRow(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    target_description: Mapped[str] = mapped_column(Text, nullable=False)
    default_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    settings_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),
        Index("ix_projects_owner", "owner_id"),
    )


class SessionRow(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    active_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 稳定映射：同一 session always 使用同一 thread_id
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    scope_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: session 事件序号分配器（在短事务内 `SELECT ... FOR UPDATE` 后递增）
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (Index("ix_sessions_project_status", "project_id", "status"),)


class RunRow(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    completion_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    price_catalog_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    owner_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    control_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    code_version: Mapped[str] = mapped_column(String(64), nullable=False, default="dev")
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="1")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resumed_from_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_runs_session_status", "session_id", "status"),
        Index("ix_runs_lease", "owner", "lease_until"),
        # 活跃 run 唯一性：同一 session 同时最多一个非终态 run。
        # 使用部分唯一索引表达，避免应用层"先查后写"的竞态。
        Index(
            "uq_runs_active_per_session",
            "session_id",
            unique=True,
            postgresql_where=_active_run_predicate(),
        ),
    )


class CommandRow(TimestampMixin, Base):
    __tablename__ = "commands"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="message")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: 幂等键在 session 内唯一；同键不同内容 -> 冲突
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    control_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_commands_session_idem"),
        Index("ix_commands_pending", "session_id", "status", "priority"),
    )


class IdentityRow(TimestampMixin, Base):
    __tablename__ = "identities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role_label: Mapped[str] = mapped_column(String(64), nullable=False, default="primary")
    #: **无明文密码列**；仅保存引用
    secret_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    auth_state_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    auth_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("project_id", "display_name", name="uq_identities_project_name"),
    )


# --------------------------------------------------------------------------- #
# 资产 / 线索 / 发现 / 验证 / 覆盖
# --------------------------------------------------------------------------- #


class AssetRow(TimestampMixin, Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 显示用归一化。**不改变原始请求**，动态参数/重复参数/编码差异保存在原件（§5.2）
    scheme: Mapped[str] = mapped_column(String(16), nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, default="/")
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="GET")
    query_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    identity_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # 去重键包含 method 与身份：**不能**盲目合并不同 HTTP 方法或忽略身份
        UniqueConstraint(
            "session_id",
            "scheme",
            "host",
            "port",
            "path",
            "method",
            "identity_key",
            name="uq_assets_endpoint_identity",
        ),
        Index("ix_assets_session_host", "session_id", "host"),
    )
    #: 身份集合的稳定序列化键（用于唯一约束；JSONB 不能作唯一键的稳定成分）
    identity_key: Mapped[str] = mapped_column(String(256), nullable=False, default="")


class LeadRow(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.leads.id", ondelete="RESTRICT"),
        nullable=True,
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    identity_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority_rationale: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: blocked/deferred 必须有理由与恢复条件（§6.5）
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 前置条件：条件 key、描述，以及可选的已有对象引用或预期产出该条件的 lead_id。
    #: 由线索关系投影，**不**另存一套可写 depends_on 图。
    required_conditions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    #: 当前能否推进（ready/needs_condition/no_new_action/needs_review）与依据指纹。
    #: 与 `status`（工作状态）是两个维度：`deferred + ready` 仍然阻止自主结束。
    actionability_assessment: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_leads_session_status_priority", "session_id", "status", "priority"),
        UniqueConstraint("id", "session_id", name="uq_leads_id_session"),
        Index("ix_leads_alias", "merged_into_id"),
        CheckConstraint(
            "status NOT IN ('blocked','deferred') OR blocked_reason IS NOT NULL",
            name="lead_status_requires_reason",
        ),
    )


class FindingRow(TimestampMixin, Base):
    """弱点实体。

    产品概念与新工具使用"弱点"，物理表继续沿用 `findings` 以保留 UUID、验证外键、
    历史报告与旧 API 兼容（方案 §3.1）。**不**另建一张与 Finding 同步写入的 Weakness 主表。
    """

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    asset_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    identity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: 只有通过验证才能从 candidate 提升为 confirmed
    verification_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    reported_in_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: `vulnerability / enabling_condition / unclassified`：区分独立缺陷与可供后续利用的条件。
    #: 旧 Finding 迁移后一律为 `unclassified`，**不**自动认定为可利用弱点。
    weakness_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="unclassified")
    #: 适用资产、身份、已知前置条件和限制；复用已有 asset_id / identity_id。
    applicability: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: 有证据引用的条件陈述（稳定 key + 简短 statement + evidence_refs + limitations）。
    #: 证据引用说明**判断依据**，不自动证明陈述成立。
    provided_conditions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    #: 模型对当前版本后续用途的短判断：依据指纹、关联线索 ID、暂未发现连接的说明、来源调用。
    connection_review: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: 实质内容的哈希。仅更新用途判断元数据**不**改变它，避免判断刚写入就使自己过期。
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_findings_session_status", "session_id", "status"),
        UniqueConstraint("id", "session_id", name="uq_findings_id_session"),
        CheckConstraint(
            "status <> 'confirmed' OR verification_id IS NOT NULL",
            name="finding_confirmed_requires_verification",
        ),
    )


class LeadWeaknessLinkRow(TimestampMixin, Base):
    """线索—弱点关系。`basis` = 线索依据哪个弱点；`result` = 线索验证产生了哪个弱点。

    **两个弱点分别成立不代表其组合已成立**：`evidence_linked` 只表示关系有引用依据，
    不等于整条组合路径已确认，也不使弱点从 candidate 变成 confirmed（方案 §3.3）。
    """

    __tablename__ = "lead_weakness_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT：禁止静默级联抹掉关系证据
        ForeignKey(f"{APP_SCHEMA}.leads.id", ondelete="RESTRICT"),
        nullable=False,
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.findings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    condition_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requirement_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 引用时的来源弱点版本；旧版本来源**不**自动升级为新版本判断。
    source_weakness_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    relation_state: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        # 关系身份唯一：空 key 统一表示，避免 NULL 造成重复行。
        ForeignKeyConstraint(["lead_id", "session_id"], [f"{APP_SCHEMA}.leads.id", f"{APP_SCHEMA}.leads.session_id"], name="fk_links_lead_session", ondelete="RESTRICT"),
        ForeignKeyConstraint(["finding_id", "session_id"], [f"{APP_SCHEMA}.findings.id", f"{APP_SCHEMA}.findings.session_id"], name="fk_links_finding_session", ondelete="RESTRICT"),
        CheckConstraint("role IN ('basis', 'result')", name="link_role_valid"),
        CheckConstraint("relation_state IN ('proposed', 'evidence_linked', 'refuted')", name="link_state_valid"),
        UniqueConstraint(
            "lead_id",
            "finding_id",
            "role",
            "condition_key",
            "requirement_key",
            name="uq_lead_weakness_links_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_lead_weakness_links_session_lead", "session_id", "lead_id"),
        Index("ix_lead_weakness_links_session_finding", "session_id", "finding_id"),
        Index("ix_lead_weakness_links_finding_condition", "finding_id", "condition_key"),
    )


class VerificationRow(TimestampMixin, Base):
    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    method_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    recheck_evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    conclusion: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 阴性结论必须附测试条件；限制写清楚，不宣称永久排除
    limitations: Mapped[str | None] = mapped_column(Text, nullable=True)
    tested_conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    executed_by_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    performed_by: Mapped[str] = mapped_column(String(32), nullable=False, default="code")

    __table_args__ = (Index("ix_verifications_finding", "finding_id"),)


class CoverageRow(TimestampMixin, Base):
    __tablename__ = "coverage"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    entry_label: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    identity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    check_category: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_checked")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        # 组合键去重：同一 资产/入口 + 身份 + 检查类别 只有一行
        UniqueConstraint(
            "session_id",
            "asset_id",
            "entry_label",
            "identity_id",
            "check_category",
            name="uq_coverage_combo",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_coverage_session_category", "session_id", "check_category", "status"),
    )


# --------------------------------------------------------------------------- #
# 工具操作 / 远端任务 / 工件 / 模型调用 / 事件 / 报告
# --------------------------------------------------------------------------- #


class ToolOperationRow(TimestampMixin, Base):
    __tablename__ = "tool_operations"

    operation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_class: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scope_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # NULL = 迁移前历史操作，不能推断它是否属于当前方向。
    dispatch_control_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    identity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    #: 崩溃恢复时按 (run_id, tool_call_id) 找回原操作 ID（§8.5）
    result_artifact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_tool_operations_run_call"),
        Index("ix_tool_operations_job", "job_id"),
        Index("ix_tool_operations_session", "session_id", "status"),
    )


class ToolJobRow(TimestampMixin, Base):
    __tablename__ = "tool_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    remote_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executor_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executor_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executor_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    output_cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 连接失联时状态不可确认 -> unknown，不当作 failed
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_sync")

    __table_args__ = (
        Index("ix_tool_jobs_state", "executor_id", "state"),
        Index("ix_tool_jobs_operation", "operation_id"),
    )


class ArtifactRow(TimestampMixin, Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 相对数据根的键；不含绝对路径
    relative_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    availability: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 采集来源时间、身份、URL/操作 —— 哈希只证明内容一致性，不证明漏洞结论
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_by_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 报告与有效发现引用的工件不能被普通清理任务删除
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 产生该工件的本地工具调用（例如 export_execution_output 的 tool_call_id）。
    #: 本地工具没有 Executor operation_id，不能把两者混为一谈。
    export_tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: execution_output / history_archive / report / import / unknown
    producer_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    #: ledger / derived / imported / unresolved
    source_trust: Mapped[str] = mapped_column(String(32), nullable=False, default="unresolved")
    #: resolved / legacy_unresolved / ambiguous
    resolution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unresolved"
    )

    __table_args__ = (
        Index("ix_artifacts_source_operation", "source_operation_id"),
        Index("ix_artifacts_hash", "sha256"),
        Index("ix_artifacts_export_call", "export_tool_call_id"),
        Index("ix_artifacts_resolution", "resolution_status", "producer_kind"),
    )


class ArtifactProvenanceRow(TimestampMixin, Base):
    """工件的来源断言：一个工件可以对应多个产生操作区间。

    只记录“这些字节来自哪里”，不把输出内容当成事实。一个导出调用
    （`export_tool_call_id`）与真正产生字节的 Executor 操作
    （`origin_operation_id`）必须分开。
    """

    __tablename__ = "artifact_provenance"

    provenance_id: Mapped[uuid.UUID] = _uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    provenance_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    segment_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    export_tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    origin_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    origin_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    stream: Mapped[str | None] = mapped_column(String(16), nullable=True)
    artifact_byte_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_byte_start: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_byte_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    identity_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connection_origin: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: execution_output / history_archive / report / import / derived
    producer_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ledger / derived / imported / unresolved
    source_trust: Mapped[str] = mapped_column(String(32), nullable=False)
    #: resolved / legacy_unresolved / ambiguous
    resolution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: receipt_verified / ledger_verified / imported / legacy
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    #: unknown / complete / partial；只描述已存区间，不推断完整输出无丢失
    total_stream_completeness: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "provenance_revision",
            "segment_no",
            name="uq_artifact_provenance_segment",
        ),
        CheckConstraint(
            "artifact_byte_start >= 0 AND artifact_byte_end > artifact_byte_start",
            name="artifact_range_valid",
        ),
        CheckConstraint(
            "source_byte_start IS NULL OR source_byte_end > source_byte_start",
            name="source_range_valid",
        ),
        Index("ix_artifact_provenance_origin", "origin_operation_id"),
        Index("ix_artifact_provenance_resource", "resource_id"),
        Index("ix_artifact_provenance_export", "export_tool_call_id"),
    )


class ArtifactLinkRow(TimestampMixin, Base):
    """归档/报告与原始工件之间的有类型引用。"""

    __tablename__ = "artifact_links"

    link_id: Mapped[uuid.UUID] = _uuid_pk()
    from_artifact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_artifact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: derived_from / archive_of / report_of
    link_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "from_artifact_id", "to_artifact_id", "link_kind", name="uq_artifact_link"
        ),
        CheckConstraint("from_artifact_id <> to_artifact_id", name="artifact_link_not_self"),
        Index("ix_artifact_links_to", "to_artifact_id", "link_kind"),
    )


class DomainIntegrationRow(TimestampMixin, Base):
    """一次业务整合尝试的窄回执；不保存计划状态。"""

    __tablename__ = "domain_integrations"

    integration_id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    source_tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: pending / integrated / retryable / conflict / rejected / superseded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_event_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    successful_domain_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_outbox_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "action", "request_hash", name="uq_domain_integration_request"
        ),
        UniqueConstraint(
            "session_id", "action", "source_tool_call_id", name="uq_domain_integration_call"
        ),
        Index("ix_domain_integrations_entity", "entity_kind", "entity_id"),
        Index("ix_domain_integrations_status", "status"),
    )


class CheckpointHeadRow(Base):
    """已接受的 checkpoint head；用于 writer fencing 与恢复选择。"""

    __tablename__ = "checkpoint_heads"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    accepted_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    writer_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    owner_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    control_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CheckpointWriteReceiptRow(Base):
    """pending writes 的 fencing 旁路收据；不推进 accepted head。"""

    __tablename__ = "checkpoint_write_receipts"

    receipt_id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    write_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    writer_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    control_version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "write_key",
            name="uq_checkpoint_write_receipt",
        ),
        Index(
            "ix_checkpoint_write_receipts_head",
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
        ),
    )


class ModelCallRow(TimestampMixin, Base):
    __tablename__ = "model_calls"

    model_call_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    logical_call_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    retry_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts_observable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    usage_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    cost_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    cost_amount: Mapped[Any | None] = mapped_column(Numeric(20, 10), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 模型可见正文的不可变工件；只供历史浏览，不回灌图状态或长期记忆。
    response_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.artifacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 持久化失败时的本地待同步标记；幂等重放（§11.3 规则 6）
    pending_sync: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_model_calls_run_phase", "run_id", "phase"),
        Index("ix_model_calls_logical", "logical_call_id"),
        Index("ix_model_calls_pending_sync", "pending_sync"),
    )


class EventRow(Base):
    __tablename__ = "events"

    #: 全局自增仅用于内部；对外的顺序语义由 (session_id, session_seq) 提供
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, unique=True)
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    session_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("session_id", "session_seq", name="uq_events_session_seq"),
        Index("ix_events_session_session_seq", "session_id", "session_seq"),
    )


class ObservationRow(TimestampMixin, Base):
    """观察记录：连接"工具结果"与"候选发现"的中间事实（§13.1）。

    观察是"我看到了什么"，发现是"这意味着什么"。两者分开，避免把观察直接升级成结论。
    """

    __tablename__ = "observations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    identity_label: Mapped[str] = mapped_column(String(128), nullable=False, default="anonymous")
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 引用**真实可用**的工件 ID；引用不存在的证据会被写入前检查拒绝
    artifact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_observations_session_time", "session_id", "observed_at"),
        Index("ix_observations_source_run", "source_run_id"),
    )


class SpanRow(Base):
    """轻量 span 表（§14.2）。

    只用于拆分耗时与定位"等的是模型、工具还是网络"。**不是** LangSmith/OTel 的替代。
    """

    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    segment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Numeric(18, 3), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_spans_run_name", "run_id", "name"),
        Index("ix_spans_session_started", "session_id", "started_at"),
    )


class ReportVersionRow(TimestampMixin, Base):
    __tablename__ = "report_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_data_revision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    captured_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifact_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    generated_by_model_call_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    generation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    markdown_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    html_artifact_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    is_historical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("session_id", "version", name="uq_report_versions_session_version"),
    )


# --------------------------------------------------------------------------- #
# 执行底座（V4）：资源只读投影 / wait-set / 委托 / 事件 / delivery / 模板
# --------------------------------------------------------------------------- #


class ExecutionResourceRow(TimestampMixin, Base):
    """会话资源在控制端的**只读投影**。

    真实执行状态归 Executor 台账；这里只同步观察到的状态，用于 UI、等待与撤销。
    原创建 run/operation 不改写（跨 run 管理会生成新操作，而不是重写资源来源）。
    """

    __tablename__ = "execution_resources"

    resource_id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    executor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="command")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="starting")
    interaction_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_applicable"
    )
    origin_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    origin_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output_limit_action: Mapped[str] = mapped_column(
        String(32), nullable=False, default="preserve_process"
    )
    quota: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: open / close_requested / closed / unknown；取消未确认时保持 close_requested
    close_state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    close_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_sync")

    __table_args__ = (
        Index("ix_execution_resources_session", "session_id", "state"),
        Index("ix_execution_resources_run", "origin_run_id"),
    )


class ExecutionWaitSetRow(TimestampMixin, Base):
    """明确等待集合：按具体任务/事件等待，默认任一匹配即可继续。"""

    __tablename__ = "execution_wait_sets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    resource_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    event_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    match: Mapped[str] = mapped_column(String(8), nullable=False, default="any")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    creator_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_execution_wait_sets_run", "run_id", "status"),
        Index("ix_execution_wait_sets_session", "session_id", "status"),
    )


class ExecutionDelegationRow(TimestampMixin, Base):
    """值班委托：资源存活与值班是**两件事**。

    - 阶段结束默认存活但未委托；
    - 取消委托只递增 revision 并终止处置，不关闭资源；
    - 只有当前用户任务明确要求持续处理时才可建立（授权来源必须在指令里可核对）。
    """

    __tablename__ = "execution_delegations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    event_filters: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="once")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    authorization_source: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_command_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    authorization_quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_execution_delegations_session", "session_id", "status"),
    )


class ExecutionEventReceiptRow(Base):
    """Executor 执行事件在控制端的**来源可追溯投影**。

    以 `(executor_id, event_id)` 幂等入库；游标只在这批事件落库成功后推进。
    """

    __tablename__ = "execution_event_receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    executor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    availability: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 已进入某个 delivery batch / 已处理（防止重复注入）
    consumed_by_batch: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("executor_id", "event_id", name="uq_execution_event_receipts_origin"),
        Index("ix_execution_event_receipts_session", "session_id", "sequence"),
        Index("ix_execution_event_receipts_unconsumed", "session_id", "consumed_by_batch"),
    )


class ExecutionEventCursorRow(TimestampMixin, Base):
    """按 executor 保存的远端读取游标。同批事件落库成功后才推进。"""

    __tablename__ = "execution_event_cursors"

    executor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ExecutionDeliveryBatchRow(TimestampMixin, Base):
    """可靠聚合交付：先准备 batch，再用稳定 message_id 注入并确认。"""

    __tablename__ = "execution_delivery_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    delegation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    event_receipt_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: prepared -> injected -> delivered；failed 表示注入流程明确失败
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    message_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    message_command_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_execution_delivery_batches_session", "session_id", "status"),
    )


class ExecutionTemplateRow(TimestampMixin, Base):
    """可复用执行模板（command/script）。注册/更新只创建新版本，不直接执行。"""

    __tablename__ = "execution_templates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="command")
    latest_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("session_id", "name", name="uq_execution_templates_session_name"),
    )


class ExecutionTemplateVersionRow(TimestampMixin, Base):
    __tablename__ = "execution_template_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(f"{APP_SCHEMA}.execution_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    script_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("template_id", "revision", name="uq_execution_template_versions_rev"),
    )


ALL_TABLES = [
    ProjectRow,
    SessionRow,
    RunRow,
    CommandRow,
    IdentityRow,
    AssetRow,
    LeadRow,
    FindingRow,
    LeadWeaknessLinkRow,
    VerificationRow,
    CoverageRow,
    ObservationRow,
    ToolOperationRow,
    ToolJobRow,
    ArtifactRow,
    ModelCallRow,
    EventRow,
    SpanRow,
    ReportVersionRow,
    ExecutionResourceRow,
    ExecutionWaitSetRow,
    ExecutionDelegationRow,
    ExecutionEventReceiptRow,
    ExecutionEventCursorRow,
    ExecutionDeliveryBatchRow,
    ExecutionTemplateRow,
    ExecutionTemplateVersionRow,
]
