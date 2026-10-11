"""为观察记录补充 run 来源，支持阶段结束时可靠提炼候选记忆。

Revision ID: e731ad4c9b02
Revises: 9c7280d9a4b1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e731ad4c9b02"
down_revision: str | None = "9c7280d9a4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "observations", sa.Column("source_run_id", sa.UUID(), nullable=True), schema="app"
    )
    op.create_foreign_key(
        "fk_observations_source_run",
        "observations",
        "runs",
        ["source_run_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_index("ix_observations_source_run", "observations", ["source_run_id"], schema="app")


def downgrade() -> None:
    op.drop_index("ix_observations_source_run", table_name="observations", schema="app")
    op.drop_constraint(
        "fk_observations_source_run", "observations", schema="app", type_="foreignkey"
    )
    op.drop_column("observations", "source_run_id", schema="app")
