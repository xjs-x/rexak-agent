"""用户附件独立身份；不改动历史记录和 checkpoint。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "a771c28e10f1"
down_revision = "c416a8d2e016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("app.sessions.id"), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source_message_id", UUID(as_uuid=True), sa.ForeignKey("app.commands.id")),
        sa.Column("upload_key", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("relative_key", sa.Text(), nullable=False),
        sa.Column("parsed_key", sa.Text()),
        sa.Column("parse_status", sa.String(32), nullable=False),
        sa.Column("metadata_json", JSONB, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "upload_key", name="uq_attachments_session_upload"),
        schema="app",
    )
    op.create_index("ix_attachments_session_message", "attachments", ["session_id", "source_message_id"], schema="app")
    op.create_index("ix_attachments_expiry", "attachments", ["status", "expires_at"], schema="app")


def downgrade() -> None:
    op.drop_table("attachments", schema="app")
