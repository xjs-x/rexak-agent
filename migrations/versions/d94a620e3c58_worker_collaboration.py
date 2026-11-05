"""Bounded collaboration events and runtime-local steering receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "d94a620e3c58"
down_revision = "c83f510d2b47"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("delegation_collaboration",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("delegation_id", pg.UUID(as_uuid=True), sa.ForeignKey("app.agent_delegations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_run_id", pg.UUID(as_uuid=True), sa.ForeignKey("app.runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("delivery_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("parent_run_id", "sequence", name="uq_collaboration_run_sequence"), schema="app")
    op.execute("""CREATE TABLE ultra_worker.messages (
        id uuid PRIMARY KEY, request_id uuid NOT NULL REFERENCES ultra_worker.requests(request_id) ON DELETE CASCADE,
        digest varchar(64) NOT NULL, payload jsonb NOT NULL, status varchar(32) NOT NULL,
        sequence bigint NOT NULL, UNIQUE(request_id,sequence),
        created_at timestamptz NOT NULL DEFAULT now())""")
    # Existing restricted runtime role gains only the private inbox, no app access.
    op.execute("""DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT grantee FROM information_schema.role_table_grants
          WHERE table_schema='ultra_worker' AND table_name='requests' AND privilege_type='INSERT'
          AND grantee <> current_user AND grantee <> 'PUBLIC'
        LOOP EXECUTE format('GRANT SELECT,INSERT,UPDATE ON ultra_worker.messages TO %I',r.grantee);
        END LOOP; END $$""")


def downgrade():
    op.execute("DROP TABLE ultra_worker.messages")
    op.drop_table("delegation_collaboration", schema="app")
