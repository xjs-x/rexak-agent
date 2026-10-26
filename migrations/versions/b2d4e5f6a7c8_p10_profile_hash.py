"""P10: coverage profile definition hash.

Revision ID: b2d4e5f6a7c8
Revises: a1c2e3f4b5d6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4e5f6a7c8"
down_revision: str | None = "a1c2e3f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coverage_profiles",
        sa.Column(
            "definition_hash", sa.String(length=64), nullable=False, server_default=""
        ),
        schema="app",
    )
    op.alter_column(
        "coverage_profiles", "definition_hash", server_default=None, schema="app"
    )


def downgrade() -> None:
    op.drop_column("coverage_profiles", "definition_hash", schema="app")
