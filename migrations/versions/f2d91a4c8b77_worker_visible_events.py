"""Incremental bounded Worker-visible execution messages."""
from alembic import op

revision = "f2d91a4c8b77"
down_revision = "e05b731f4d69"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE ultra_worker.visible_events (
        request_id uuid NOT NULL REFERENCES ultra_worker.requests(request_id) ON DELETE CASCADE,
        sequence integer NOT NULL,
        role text NOT NULL,
        message jsonb NOT NULL,
        sha256 varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (request_id, sequence))""")
    op.execute("""DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT grantee FROM information_schema.role_table_grants
          WHERE table_schema='ultra_worker' AND table_name='requests' AND privilege_type='INSERT'
          AND grantee <> current_user AND grantee <> 'PUBLIC'
        LOOP EXECUTE format('GRANT SELECT,INSERT ON ultra_worker.visible_events TO %I',r.grantee);
        END LOOP; END $$""")


def downgrade():
    op.execute("DROP TABLE ultra_worker.visible_events")
