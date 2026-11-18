"""Allow durable Worker message drafts to become completed events."""

from alembic import op

revision = "ab71c0d28e44"
down_revision = "c8a7d65f021b"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE app.skill_installations ADD COLUMN archived boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE ultra_worker.visible_events ADD COLUMN phase text NOT NULL DEFAULT 'complete'")
    op.execute("ALTER TABLE ultra_worker.visible_events ADD COLUMN revision integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE ultra_worker.visible_events ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()")
    op.execute("""DO $$ DECLARE r record; BEGIN
        FOR r IN SELECT grantee FROM information_schema.role_table_grants
          WHERE table_schema='ultra_worker' AND table_name='requests' AND privilege_type='INSERT'
          AND grantee <> current_user AND grantee <> 'PUBLIC'
        LOOP EXECUTE format('GRANT UPDATE ON ultra_worker.visible_events TO %I',r.grantee);
        END LOOP; END $$""")


def downgrade():
    op.execute("ALTER TABLE app.skill_installations DROP COLUMN archived")
    op.execute("ALTER TABLE ultra_worker.visible_events DROP COLUMN updated_at")
    op.execute("ALTER TABLE ultra_worker.visible_events DROP COLUMN revision")
    op.execute("ALTER TABLE ultra_worker.visible_events DROP COLUMN phase")
