"""线索别名与派发时上下文；历史记录保持 NULL。

Revision ID: a193bc402e61
Revises: f42cb7a9e180
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "a193bc402e61"
down_revision: str | None = "f42cb7a9e180"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("merged_into_id", UUID(), nullable=True), schema="app")
    op.create_foreign_key(
        "fk_leads_alias",
        "leads",
        "leads",
        ["merged_into_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    op.create_index("ix_leads_alias", "leads", ["merged_into_id"], schema="app")
    for name in ("execution_context", "result_snapshot"):
        op.add_column("tool_operations", sa.Column(name, JSONB(), nullable=True), schema="app")
    op.add_column(
        "report_versions", sa.Column("captured_snapshot", JSONB(), nullable=True), schema="app"
    )


def downgrade() -> None:
    op.drop_column("report_versions", "captured_snapshot", schema="app")
    for name in ("result_snapshot", "execution_context"):
        op.drop_column("tool_operations", name, schema="app")
    op.drop_index("ix_leads_alias", table_name="leads", schema="app")
    op.drop_constraint("fk_leads_alias", "leads", schema="app", type_="foreignkey")
    op.drop_column("leads", "merged_into_id", schema="app")
