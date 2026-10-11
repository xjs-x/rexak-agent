"""保存执行岛返回的任务时间戳，用于区分排队与实际执行区间。

Revision ID: b7d90c62e4a1
Revises: e731ad4c9b02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d90c62e4a1"
down_revision: str | None = "e731ad4c9b02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("executor_accepted_at", "executor_started_at", "executor_finished_at"):
        op.add_column(
            "tool_jobs", sa.Column(name, sa.DateTime(timezone=True), nullable=True), schema="app"
        )


def downgrade() -> None:
    for name in ("executor_finished_at", "executor_started_at", "executor_accepted_at"):
        op.drop_column("tool_jobs", name, schema="app")
