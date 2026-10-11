"""保存不可变价格目录版本，避免修改历史模型调用的费用依据。

Revision ID: ad6e4812c0f7
Revises: d0ae471c18f0
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "ad6e4812c0f7"
down_revision: str | None = "d0ae471c18f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_catalog_versions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("version", sa.String(length=64), nullable=False, unique=True),
        sa.Column("prices", JSONB(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="app",
    )
    op.create_index(
        "uq_price_catalog_current",
        "price_catalog_versions",
        ["is_current"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("is_current = true"),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO app.price_catalog_versions (id, version, prices, is_current) "
            "VALUES (:id, :version, CAST(:prices AS jsonb), true)"
        ),
        {
            "id": UUID("00000000-0000-0000-0000-000000000001"),
            "version": "2026-10-08.1",
            "prices": "[]",
        },
    )


def downgrade() -> None:
    op.drop_index("uq_price_catalog_current", table_name="price_catalog_versions", schema="app")
    op.drop_table("price_catalog_versions", schema="app")
