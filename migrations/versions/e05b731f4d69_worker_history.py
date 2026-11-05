"""Completed Pi transcripts, separate from executable task state."""
from alembic import op

revision = "e05b731f4d69"
down_revision = "d94a620e3c58"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE ultra_worker.histories (
        request_id uuid PRIMARY KEY REFERENCES ultra_worker.requests(request_id) ON DELETE CASCADE,
        transcript jsonb NOT NULL, sha256 varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT grantee FROM information_schema.role_table_grants
          WHERE table_schema='ultra_worker' AND table_name='requests' AND privilege_type='INSERT'
          AND grantee <> current_user AND grantee <> 'PUBLIC'
        LOOP EXECUTE format('GRANT SELECT,INSERT ON ultra_worker.histories TO %I',r.grantee);
        END LOOP; END $$""")


def downgrade():
    op.execute("DROP TABLE ultra_worker.histories")
