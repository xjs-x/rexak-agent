"""P1-D2: switch asset uniqueness to canonical endpoint.

Revision ID: f5c7d0e3b442
Revises: e4b6c9d2a331
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f5c7d0e3b442"
down_revision: str | None = "e4b6c9d2a331"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_assets_endpoint_identity", "assets", schema="app", type_="unique")
    op.drop_constraint("uq_assets_canonical_endpoint", "assets", schema="app", type_="unique")
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY session_id, scheme, host, port,
                         CASE WHEN strpos(path, '?') > 0
                              THEN left(path, strpos(path, '?') - 1)
                              ELSE path END,
                         method
                       ORDER BY created_at, id
                   ) AS rn,
                   first_value(id) OVER (
                       PARTITION BY session_id, scheme, host, port,
                         CASE WHEN strpos(path, '?') > 0
                              THEN left(path, strpos(path, '?') - 1)
                              ELSE path END,
                         method
                       ORDER BY created_at, id
                   ) AS canonical_id
            FROM app.assets
        )
        UPDATE app.assets AS a
        SET path_component = CASE WHEN strpos(a.path, '?') > 0
                                  THEN left(a.path, strpos(a.path, '?') - 1)
                                  ELSE a.path END,
            method_token = a.method,
            normalizer_version = 'legacy_v1',
            record_role = CASE WHEN r.rn = 1 THEN 'canonical' ELSE 'alias' END,
            canonical_asset_id = CASE WHEN r.rn = 1 THEN NULL ELSE r.canonical_id END
        FROM ranked AS r
        WHERE a.id = r.id
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_assets_canonical_endpoint "
        "ON app.assets (session_id, scheme, host, port, path_component, method_token) "
        "WHERE record_role = 'canonical'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.uq_assets_canonical_endpoint")
    # 迁移后的 alias 行可能共享规范键；降级不强行合并或删除历史行。
    op.create_unique_constraint(
        "uq_assets_endpoint_identity",
        "assets",
        ["session_id", "scheme", "host", "port", "path", "method", "identity_key"],
        schema="app",
    )
