"""Restore the identity-aware uniqueness required by the production asset writer.

Revision ID: f19a30b742c1
Revises: e82d934a10bc
"""

from alembic import op

revision = "f19a30b742c1"
down_revision = "e82d934a10bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No history is merged or deleted. Duplicate historical keys fail the migration
    # transaction explicitly, leaving the previous schema and data intact.
    op.execute("DROP INDEX IF EXISTS app.uq_assets_canonical_endpoint")
    op.create_unique_constraint(
        "uq_assets_endpoint_identity", "assets",
        ["session_id", "scheme", "host", "port", "path", "method", "identity_key"],
        schema="app",
    )


def downgrade() -> None:
    # Different identities may now share a canonical endpoint. Refuse unsafe
    # downgrades rather than changing or silently merging those records.
    op.drop_constraint("uq_assets_endpoint_identity", "assets", schema="app", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_assets_canonical_endpoint "
        "ON app.assets (session_id, scheme, host, port, path_component, method_token) "
        "WHERE record_role = 'canonical'"
    )
