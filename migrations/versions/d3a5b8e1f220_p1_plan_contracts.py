"""P1-A: immutable TaskContract versions.

Revision ID: d3a5b8e1f220
Revises: c2f4a7d9e110
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "d3a5b8e1f220"
down_revision: str | None = "c2f4a7d9e110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_contracts",
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("contract_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("contract_revision", sa.Integer(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parent_contract_id", UUID(as_uuid=True), nullable=True),
        sa.Column("parent_contract_revision", sa.Integer(), nullable=True),
        sa.Column("goal_ref", sa.String(length=256), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="mixed"),
        sa.Column("lead_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("requires_json", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "expected_outcomes_json", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("produces_json", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("acceptance_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="proposed"),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("resume_when_json", JSONB, nullable=True),
        sa.Column("basis_versions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("acceptance_result_ref", sa.String(length=128), nullable=True),
        sa.Column("mutation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("based_on_checkpoint_ref", sa.String(length=256), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "session_id",
            "mutation_id",
            "contract_id",
            name="uq_plan_contract_mutation",
        ),
        sa.CheckConstraint("contract_revision > 0", name="contract_revision_positive"),
        schema="app",
    )
    op.create_index(
        "ix_plan_contracts_goal",
        "plan_contracts",
        ["session_id", "goal_ref"],
        schema="app",
    )
    op.create_index(
        "ix_plan_contracts_parent",
        "plan_contracts",
        ["session_id", "parent_contract_id", "parent_contract_revision"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_plan_contracts_parent", table_name="plan_contracts", schema="app")
    op.drop_index("ix_plan_contracts_goal", table_name="plan_contracts", schema="app")
    op.drop_table("plan_contracts", schema="app")
