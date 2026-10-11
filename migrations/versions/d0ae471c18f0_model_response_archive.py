"""把模型正文工件关联到物理模型调用。

Revision ID: d0ae471c18f0
Revises: b7d90c62e4a1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0ae471c18f0"
down_revision: str | None = "b7d90c62e4a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_calls",
        sa.Column("response_artifact_id", sa.UUID(), nullable=True),
        schema="app",
    )
    op.create_foreign_key(
        "fk_model_calls_response_artifact_id_artifacts",
        "model_calls",
        "artifacts",
        ["response_artifact_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_model_calls_response_artifact_id_artifacts", "model_calls", schema="app"
    )
    op.drop_column("model_calls", "response_artifact_id", schema="app")
