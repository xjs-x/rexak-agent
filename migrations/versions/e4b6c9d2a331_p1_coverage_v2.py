"""P1-C: typed assets, request variants, parameters, coverage v2 columns.

Revision ID: e4b6c9d2a331
Revises: d3a5b8e1f220
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "e4b6c9d2a331"
down_revision: str | None = "d3a5b8e1f220"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("record_role", sa.String(length=32), nullable=False, server_default="canonical"),
        schema="app",
    )
    op.add_column("assets", sa.Column("canonical_asset_id", UUID(as_uuid=True), nullable=True), schema="app")
    op.add_column("assets", sa.Column("path_component", sa.Text(), nullable=True), schema="app")
    op.add_column("assets", sa.Column("method_token", sa.String(length=32), nullable=True), schema="app")
    op.add_column("assets", sa.Column("endpoint_key", sa.String(length=64), nullable=True), schema="app")
    op.add_column("assets", sa.Column("normalizer_version", sa.String(length=32), nullable=True), schema="app")
    op.add_column("assets", sa.Column("legacy_snapshot_ref", sa.String(length=128), nullable=True), schema="app")
    op.create_foreign_key(
        "fk_assets_canonical_asset",
        "assets",
        "assets",
        ["canonical_asset_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_assets_canonical_endpoint",
        "assets",
        ["session_id", "scheme", "host", "port", "path_component", "method_token"],
        schema="app",
    )
    op.create_index("ix_assets_canonical", "assets", ["session_id", "canonical_asset_id"], schema="app")

    op.create_table(
        "request_variants",
        sa.Column("variant_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("raw_url", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False, server_default="/"),
        sa.Column("query_string", sa.Text(), nullable=True),
        sa.Column("has_query_delimiter", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("method_token", sa.String(length=32), nullable=True),
        sa.Column("identity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("normalizer_version", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="app",
    )
    op.create_index(
        "ix_request_variants_asset",
        "request_variants",
        ["session_id", "asset_id"],
        schema="app",
    )
    op.create_index(
        "ix_request_variants_source",
        "request_variants",
        ["source_artifact_id"],
        schema="app",
    )

    op.create_table(
        "parameters",
        sa.Column("parameter_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("location", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("occurrence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("normalizer_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "session_id",
            "asset_id",
            "location",
            "name",
            "position",
            "occurrence",
            name="uq_parameter_instance",
        ),
        schema="app",
    )
    op.create_index(
        "ix_parameters_asset", "parameters", ["session_id", "asset_id"], schema="app"
    )

    op.add_column(
        "coverage",
        sa.Column("subject_kind", sa.String(length=32), nullable=False, server_default="asset"),
        schema="app",
    )
    op.add_column("coverage", sa.Column("parameter_id", UUID(as_uuid=True), nullable=True), schema="app")
    op.add_column("coverage", sa.Column("identity_revision", sa.Integer(), nullable=True), schema="app")
    op.add_column("coverage", sa.Column("condition_fingerprint", sa.String(length=64), nullable=True), schema="app")
    op.add_column("coverage", sa.Column("profile_id", sa.String(length=64), nullable=True), schema="app")
    op.add_column("coverage", sa.Column("profile_version", sa.String(length=32), nullable=True), schema="app")
    op.add_column(
        "coverage",
        sa.Column("freshness", sa.String(length=32), nullable=False, server_default="current"),
        schema="app",
    )
    op.add_column(
        "coverage",
        sa.Column("resolution_status", sa.String(length=32), nullable=False, server_default="resolved"),
        schema="app",
    )
    op.add_column("coverage", sa.Column("evaluation_ref", sa.String(length=128), nullable=True), schema="app")
    op.create_foreign_key(
        "fk_coverage_parameter",
        "coverage",
        "parameters",
        ["parameter_id"],
        ["parameter_id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_coverage_cell_v2 ON app.coverage "
        "(session_id, asset_id, parameter_id, identity_id, identity_revision, "
        "check_category, condition_fingerprint, profile_id, profile_version) "
        "NULLS NOT DISTINCT WHERE profile_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.uq_coverage_cell_v2")
    op.drop_constraint("fk_coverage_parameter", "coverage", schema="app", type_="foreignkey")
    for column in (
        "evaluation_ref",
        "resolution_status",
        "freshness",
        "profile_version",
        "profile_id",
        "condition_fingerprint",
        "identity_revision",
        "parameter_id",
        "subject_kind",
    ):
        op.drop_column("coverage", column, schema="app")
    op.drop_index("ix_parameters_asset", table_name="parameters", schema="app")
    op.drop_table("parameters", schema="app")
    op.drop_index("ix_request_variants_source", table_name="request_variants", schema="app")
    op.drop_index("ix_request_variants_asset", table_name="request_variants", schema="app")
    op.drop_table("request_variants", schema="app")
    op.drop_index("ix_assets_canonical", table_name="assets", schema="app")
    op.drop_constraint("uq_assets_canonical_endpoint", "assets", schema="app", type_="unique")
    op.drop_constraint("fk_assets_canonical_asset", "assets", schema="app", type_="foreignkey")
    for column in (
        "legacy_snapshot_ref",
        "normalizer_version",
        "endpoint_key",
        "method_token",
        "path_component",
        "canonical_asset_id",
        "record_role",
    ):
        op.drop_column("assets", column, schema="app")
