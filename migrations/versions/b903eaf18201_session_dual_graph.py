"""Session 双图与原子修订回执，纯加法迁移。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "b903eaf18201"
down_revision = "a8b3d5f7c012"
branch_labels = None
depends_on = None


def stamps():
    return [sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
            for name in ("created_at", "updated_at")]


def upgrade() -> None:
    op.create_table("session_graphs",
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("app.sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        *[sa.Column(name, sa.BigInteger(), nullable=False) for name in
          ("reviewed_seq", "completed_rounds", "reviewed_rounds", "next_sync_round")],
        sa.Column("missed_syncs", sa.Integer(), nullable=False), *stamps(), schema="app")
    op.create_table("session_graph_receipts",
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("app.sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("request_id", sa.String(256), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("receipt", JSONB(), nullable=False), *stamps(), schema="app")


def downgrade() -> None:
    op.drop_table("session_graph_receipts", schema="app")
    op.drop_table("session_graphs", schema="app")
