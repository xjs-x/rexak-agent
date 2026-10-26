"""P9: frozen coverage profile definitions.

Revision ID: a1c2e3f4b5d6
Revises: f5c7d0e3b442
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "a1c2e3f4b5d6"
down_revision: str | None = "f5c7d0e3b442"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coverage_profiles",
        sa.Column("profile_row_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("profile_version", sa.String(length=32), nullable=False),
        sa.Column("condition_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("categories", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "include_parameter_cells", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("include_anonymous", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "session_id",
            "profile_id",
            "profile_version",
            "condition_fingerprint",
            name="uq_coverage_profile",
        ),
        schema="app",
    )
    op.create_index(
        "ix_coverage_profiles_session",
        "coverage_profiles",
        ["session_id", "created_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_coverage_profiles_session", table_name="coverage_profiles", schema="app")
    op.drop_table("coverage_profiles", schema="app")
