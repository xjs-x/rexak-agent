"""Skill 安装与不可变版本；保留所有既有任务与工件。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f7a2c4e6b901"
down_revision = "e5a1c3d7b902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_installations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_version_id", UUID(as_uuid=True)),
        sa.Column("project_ids", JSONB, nullable=False),
        sa.Column("default_for_projects", JSONB, nullable=False),
        sa.UniqueConstraint("owner_id", "source", name="uq_skills_owner_source"), schema="app",
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("installation_id", UUID(as_uuid=True), sa.ForeignKey("app.skill_installations.id"), nullable=False),
        sa.Column("package_hash", sa.String(64), nullable=False),
        sa.Column("artifact_id", UUID(as_uuid=True), sa.ForeignKey("app.artifacts.id"), nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("installation_id", "package_hash", name="uq_skill_version_content"), schema="app",
    )


def downgrade() -> None:
    op.drop_table("skill_versions", schema="app")
    op.drop_table("skill_installations", schema="app")
