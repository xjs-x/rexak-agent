"""Set the database default for new sessions to Ultra without changing old rows."""

from alembic import op

revision = "c8a7d65f021b"
down_revision = "f2d91a4c8b77"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE app.sessions ALTER COLUMN execution_mode SET DEFAULT 'ultra'")


def downgrade():
    op.execute("ALTER TABLE app.sessions ALTER COLUMN execution_mode SET DEFAULT 'standard'")
