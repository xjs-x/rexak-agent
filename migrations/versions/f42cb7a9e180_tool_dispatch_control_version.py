"""记录工具操作派发时的控制版本，用于识别方向变更前的旧 job。

旧操作无法可靠推断版本，保持 NULL 并在读取时明确标记为未知。

Revision ID: f42cb7a9e180
Revises: d6930a48e21f
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f42cb7a9e180"
down_revision: str | None = "d6930a48e21f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_operations",
        sa.Column("dispatch_control_version", sa.Integer(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("tool_operations", "dispatch_control_version", schema="app")
