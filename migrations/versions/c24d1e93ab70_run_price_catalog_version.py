"""把价格版本固定在 run 上，恢复同一 run 时保持原费用口径。

Revision ID: c24d1e93ab70
Revises: ad6e4812c0f7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c24d1e93ab70"
down_revision: str | None = "ad6e4812c0f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("price_catalog_version", sa.String(length=64), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("runs", "price_catalog_version", schema="app")
