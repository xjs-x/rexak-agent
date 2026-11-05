"""Ultra mode and durable delegation/task stores. Additive; preserves historical data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "a31c90f04d12"
down_revision = "f19a30b742c1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("execution_mode", sa.String(16), nullable=False, server_default="standard"), schema="app")
    op.create_check_constraint("ck_sessions_session_execution_mode", "sessions", "execution_mode IN ('standard', 'ultra')", schema="app")
    op.execute("UPDATE app.runs SET config_snapshot = jsonb_set(config_snapshot, '{execution_mode}', '\"standard\"'::jsonb) WHERE NOT config_snapshot ? 'execution_mode'")
    op.create_table("agent_delegations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_session_id", pg.UUID(as_uuid=True), sa.ForeignKey("app.sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_run_id", pg.UUID(as_uuid=True), sa.ForeignKey("app.runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_tool_call_id", sa.String(256), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("a2a_task_id", sa.String(128)),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("context_refs", pg.JSONB(), nullable=False),
        sa.Column("authorization_snapshot", pg.JSONB(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("remote_state", sa.String(64)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("result_artifact_id", pg.UUID(as_uuid=True)),
        sa.Column("diagnostic", sa.String(256)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("parent_run_id", "parent_tool_call_id", name="uq_agent_delegation_call"),
        sa.UniqueConstraint("a2a_task_id", name="uq_agent_delegation_a2a_task"), schema="app")
    op.create_index("ix_agent_delegations_run_state", "agent_delegations", ["parent_run_id", "state"], schema="app")
    # Worker runtime owns these rows. Python never infers Pi checkpoint state from them.
    op.execute("CREATE SCHEMA ultra_worker")
    op.execute("CREATE TABLE ultra_worker.tasks (id text PRIMARY KEY, task jsonb NOT NULL)")
    op.execute("CREATE TABLE ultra_worker.requests (request_id uuid PRIMARY KEY, digest text NOT NULL, task_id text UNIQUE NOT NULL, worker_id uuid NOT NULL, payload jsonb NOT NULL, phase text NOT NULL DEFAULT 'reserved', cancel_requested boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now())")


def downgrade():
    # Refuse to discard unresolved work during downgrade.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM app.agent_delegations WHERE state NOT IN ('completed','failed','cancelled','rejected')) THEN RAISE EXCEPTION 'Unresolved Ultra delegations'; END IF; END $$")
    op.execute("DROP SCHEMA ultra_worker CASCADE")
    op.drop_table("agent_delegations", schema="app")
    op.drop_constraint("ck_sessions_session_execution_mode", "sessions", schema="app", type_="check")
    op.drop_column("sessions", "execution_mode", schema="app")
