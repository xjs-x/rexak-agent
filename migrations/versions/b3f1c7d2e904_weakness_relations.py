"""弱点关系驱动的持续探索：Finding 升级为弱点、线索条件、线索—弱点关系表。

方案：`docs/plans/implementation-v3-weakness-chains.md` §3 / §8 / §10.1。

要点：
- 新字段使用空/未知默认值；旧 Finding **不**批量认定为可利用弱点，也不自动生成关系；
- 旧 blocked/deferred 保留原文与状态；缺失可推进性判断时由领域层归为 needs_review；
- 新关系表初始为空；旧验证外键、报告版本、工件 ID 与操作台账保持不变。

Revision ID: b3f1c7d2e904
Revises: a193bc402e61
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b3f1c7d2e904"
down_revision: str | None = "a193bc402e61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- findings：升级为弱点实体（复用原表，保留 UUID / 验证外键 / 历史报告） ---
    op.add_column(
        "findings",
        sa.Column(
            "weakness_kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unclassified'"),
        ),
        schema="app",
    )
    op.add_column(
        "findings",
        sa.Column(
            "applicability",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema="app",
    )
    op.add_column(
        "findings",
        sa.Column(
            "provided_conditions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema="app",
    )
    op.add_column(
        "findings", sa.Column("connection_review", JSONB(), nullable=True), schema="app"
    )
    op.add_column(
        "findings", sa.Column("content_hash", sa.String(length=64), nullable=True), schema="app"
    )

    # --- leads：前置条件与可推进性判断（与 status 是两个维度） ---
    op.add_column(
        "leads",
        sa.Column(
            "required_conditions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema="app",
    )
    op.add_column(
        "leads", sa.Column("actionability_assessment", JSONB(), nullable=True), schema="app"
    )

    # --- 线索—弱点关系表 ---
    op.create_table(
        "lead_weakness_links",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("condition_key", sa.String(length=64), nullable=True),
        sa.Column("requirement_key", sa.String(length=64), nullable=True),
        sa.Column("source_weakness_revision", sa.Integer(), nullable=True),
        sa.Column(
            "relation_state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column(
            "evidence_refs",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("source_operation_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["app.sessions.id"],
            name="fk_lead_weakness_links_session_id_sessions",
            ondelete="CASCADE",
        ),
        # RESTRICT：删除端点不得静默抹掉关系证据
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["app.leads.id"],
            name="fk_lead_weakness_links_lead_id_leads",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["app.findings.id"],
            name="fk_lead_weakness_links_finding_id_findings",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lead_weakness_links"),
        # 空 key 统一表示，避免 NULL 造成重复关系
        sa.UniqueConstraint(
            "lead_id",
            "finding_id",
            "role",
            "condition_key",
            "requirement_key",
            name="uq_lead_weakness_links_identity",
            postgresql_nulls_not_distinct=True,
        ),
        schema="app",
    )
    op.create_index(
        "ix_lead_weakness_links_session_lead",
        "lead_weakness_links",
        ["session_id", "lead_id"],
        schema="app",
    )
    op.create_index(
        "ix_lead_weakness_links_session_finding",
        "lead_weakness_links",
        ["session_id", "finding_id"],
        schema="app",
    )
    op.create_index(
        "ix_lead_weakness_links_finding_condition",
        "lead_weakness_links",
        ["finding_id", "condition_key"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_lead_weakness_links_finding_condition", table_name="lead_weakness_links", schema="app"
    )
    op.drop_index(
        "ix_lead_weakness_links_session_finding", table_name="lead_weakness_links", schema="app"
    )
    op.drop_index(
        "ix_lead_weakness_links_session_lead", table_name="lead_weakness_links", schema="app"
    )
    op.drop_table("lead_weakness_links", schema="app")
    op.drop_column("leads", "actionability_assessment", schema="app")
    op.drop_column("leads", "required_conditions", schema="app")
    op.drop_column("findings", "content_hash", schema="app")
    op.drop_column("findings", "connection_review", schema="app")
    op.drop_column("findings", "provided_conditions", schema="app")
    op.drop_column("findings", "applicability", schema="app")
    op.drop_column("findings", "weakness_kind", schema="app")
