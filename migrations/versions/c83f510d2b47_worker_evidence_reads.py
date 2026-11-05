"""Task-bound evidence read receipts; content remains in existing CAS."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "c83f510d2b47"
down_revision = "b72e409c1a36"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("delegation_evidence_reads",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("delegation_id", pg.UUID(as_uuid=True), sa.ForeignKey("app.agent_delegations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_call_id", sa.String(256), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("artifact_id", pg.UUID(as_uuid=True)),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("response_artifact_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("delegation_id", "tool_call_id", name="uq_delegation_evidence_call"), schema="app")


def downgrade():
    op.drop_table("delegation_evidence_reads", schema="app")
