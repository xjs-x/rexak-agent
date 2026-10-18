"""P0-D: checkpoint pending-write receipts for fencing.

Revision ID: c2f4a7d9e110
Revises: b4e6f8a1c203
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c2f4a7d9e110"
down_revision: str | None = "b4e6f8a1c203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkpoint_write_receipts",
        sa.Column("receipt_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("write_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("writer_epoch", sa.BigInteger(), nullable=False),
        sa.Column("owner_generation", sa.BigInteger(), nullable=False),
        sa.Column("control_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "write_key",
            name="uq_checkpoint_write_receipt",
        ),
        schema="app",
    )
    op.create_index(
        "ix_checkpoint_write_receipts_head",
        "checkpoint_write_receipts",
        ["thread_id", "checkpoint_ns", "checkpoint_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_checkpoint_write_receipts_head",
        table_name="checkpoint_write_receipts",
        schema="app",
    )
    op.drop_table("checkpoint_write_receipts", schema="app")
