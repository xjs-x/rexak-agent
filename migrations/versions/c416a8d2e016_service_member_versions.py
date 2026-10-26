"""仅增加成员版本存储；不迁移历史合同、coverage 或 checkpoint。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c416a8d2e016"
down_revision = "a132c5e9d013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("service_member_versions",
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("app.sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("membership_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("service_contract_id", UUID(as_uuid=True), nullable=False),
        sa.Column("member_kind", sa.String(32), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("source_refs", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision > 0", name="member_revision_positive"), schema="app")
    op.create_index("ix_service_members_scope", "service_member_versions",
                    ["session_id", "service_contract_id", "member_id"], schema="app")


def downgrade() -> None:
    raise RuntimeError("成员版本用于历史审计，禁止删除")
