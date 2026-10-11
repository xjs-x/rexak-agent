"""给 session 保存当前测试身份。

Revision ID: 9c7280d9a4b1
Revises: f11850acfff1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c7280d9a4b1"
down_revision: str | None = "f11850acfff1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("active_identity_id", sa.UUID(), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_sessions_active_identity",
        "sessions",
        "identities",
        ["active_identity_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sessions_active_identity", "sessions", schema="app", type_="foreignkey")
    op.drop_column("sessions", "active_identity_id", schema="app")
