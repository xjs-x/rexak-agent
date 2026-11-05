"""Ultra Goal handoff snapshots and append-only model call measurements."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b72e409c1a36"
down_revision = "a31c90f04d12"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_delegations", sa.Column("context_id", pg.UUID(as_uuid=True)), schema="app")
    op.add_column("agent_delegations", sa.Column("handoff_snapshot", pg.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")), schema="app")
    op.execute("""CREATE TABLE ultra_worker.model_calls (
        request_id uuid NOT NULL REFERENCES ultra_worker.requests(request_id),
        sequence integer NOT NULL, context_id text NOT NULL, provider text NOT NULL, model text NOT NULL,
        started_at timestamptz NOT NULL, finished_at timestamptz, metric jsonb,
        PRIMARY KEY(request_id, sequence))""")
    # The existing restricted service role owns no schema DDL; grant only this new table.
    op.execute("""DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT grantee FROM information_schema.role_table_grants
          WHERE table_schema='ultra_worker' AND table_name='requests' AND privilege_type='INSERT'
          AND grantee <> current_user AND grantee <> 'PUBLIC'
        LOOP EXECUTE format('GRANT SELECT,INSERT,UPDATE ON ultra_worker.model_calls TO %I',r.grantee);
        END LOOP; END $$""")


def downgrade():
    op.execute("DROP TABLE ultra_worker.model_calls")
    op.drop_column("agent_delegations", "handoff_snapshot", schema="app")
    op.drop_column("agent_delegations", "context_id", schema="app")
