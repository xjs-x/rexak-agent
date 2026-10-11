"""从首个物理模型调用回填旧 run 的价格版本。

Revision ID: d6930a48e21f
Revises: c24d1e93ab70
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6930a48e21f"
down_revision: str | None = "c24d1e93ab70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE app.runs AS r SET price_catalog_version = previous.version "
            "FROM ("
            "SELECT DISTINCT ON (mc.run_id) mc.run_id, "
            "mc.price_snapshot->>'catalog_version' AS version "
            "FROM app.model_calls AS mc "
            "JOIN app.price_catalog_versions AS pc "
            "ON pc.version = mc.price_snapshot->>'catalog_version' "
            "ORDER BY mc.run_id, mc.submitted_at NULLS LAST, mc.created_at, mc.model_call_id"
            ") AS previous "
            "WHERE r.id = previous.run_id AND r.price_catalog_version IS NULL"
        )
    )


def downgrade() -> None:
    # 回填是既有调用的事实投影，回滚迁移不应抹除已经固定的版本。
    pass
