"""有界二进制静态分析记录；只新增表，不修改历史附件或运行数据。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "d240a68c917e"
down_revision = "a771c28e10f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachment_analyses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "attachment_id", UUID(as_uuid=True), sa.ForeignKey("app.attachments.id"), nullable=False
        ),
        sa.Column(
            "session_id", UUID(as_uuid=True), sa.ForeignKey("app.sessions.id"), nullable=False
        ),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("analyzer", sa.String(64), nullable=False),
        sa.Column("analyzer_version", sa.String(256), nullable=False),
        sa.Column("params_hash", sa.String(64), nullable=False),
        sa.Column("params_json", JSONB, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", JSONB, nullable=False),
        sa.Column("result_key", sa.Text()),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("error_type", sa.String(128)),
        sa.Column("attempt_id", UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "attachment_id",
            "sha256",
            "kind",
            "analyzer",
            "analyzer_version",
            "params_hash",
            name="uq_attachment_analysis_cache",
        ),
        schema="app",
    )
    op.create_index(
        "ix_attachment_analyses_session",
        "attachment_analyses",
        ["session_id", "attachment_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("attachment_analyses", schema="app")
