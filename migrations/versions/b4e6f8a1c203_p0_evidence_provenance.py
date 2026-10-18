"""P0 evidence provenance: artifact provenance, links, domain integrations, checkpoint heads.

This migration only adds tables/columns. It does not rewrite historical rows; the
backfill script is responsible for uniquely provable legacy origins.

Revision ID: b4e6f8a1c203
Revises: a8b3d5f7c012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b4e6f8a1c203"
down_revision: str | None = "a8b3d5f7c012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("export_tool_call_id", sa.String(length=128), nullable=True),
        schema="app",
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "producer_kind", sa.String(length=32), nullable=False, server_default="unknown"
        ),
        schema="app",
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "source_trust", sa.String(length=32), nullable=False, server_default="unresolved"
        ),
        schema="app",
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "resolution_status",
            sa.String(length=32),
            nullable=False,
            server_default="unresolved",
        ),
        schema="app",
    )
    op.create_index(
        "ix_artifacts_export_call",
        "artifacts",
        ["export_tool_call_id"],
        schema="app",
    )
    op.create_index(
        "ix_artifacts_resolution",
        "artifacts",
        ["resolution_status", "producer_kind"],
        schema="app",
    )

    op.create_table(
        "artifact_provenance",
        sa.Column("provenance_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provenance_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("segment_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("export_tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("origin_operation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("origin_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=True),
        sa.Column("stream", sa.String(length=16), nullable=True),
        sa.Column("artifact_byte_start", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("artifact_byte_end", sa.BigInteger(), nullable=False),
        sa.Column("source_byte_start", sa.BigInteger(), nullable=True),
        sa.Column("source_byte_end", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("identity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("identity_revision", sa.Integer(), nullable=True),
        sa.Column("connection_origin", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("producer_kind", sa.String(length=32), nullable=False),
        sa.Column("source_trust", sa.String(length=32), nullable=False),
        sa.Column("resolution_status", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column(
            "total_stream_completeness",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "artifact_id",
            "provenance_revision",
            "segment_no",
            name="uq_artifact_provenance_segment",
        ),
        sa.CheckConstraint(
            "artifact_byte_start >= 0 AND artifact_byte_end > artifact_byte_start",
            name="artifact_range_valid",
        ),
        sa.CheckConstraint(
            "source_byte_start IS NULL OR source_byte_end > source_byte_start",
            name="source_range_valid",
        ),
        schema="app",
    )
    op.create_index(
        "ix_artifact_provenance_origin",
        "artifact_provenance",
        ["origin_operation_id"],
        schema="app",
    )
    op.create_index(
        "ix_artifact_provenance_resource",
        "artifact_provenance",
        ["resource_id"],
        schema="app",
    )
    op.create_index(
        "ix_artifact_provenance_export",
        "artifact_provenance",
        ["export_tool_call_id"],
        schema="app",
    )

    op.create_table(
        "artifact_links",
        sa.Column("link_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "from_artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("link_kind", sa.String(length=32), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "from_artifact_id", "to_artifact_id", "link_kind", name="uq_artifact_link"
        ),
        sa.CheckConstraint(
            "from_artifact_id <> to_artifact_id",
            name="artifact_link_not_self",
        ),
        schema="app",
    )
    op.create_index(
        "ix_artifact_links_to",
        "artifact_links",
        ["to_artifact_id", "link_kind"],
        schema="app",
    )

    op.create_table(
        "domain_integrations",
        sa.Column("integration_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("source_tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("result_event_seq", sa.BigInteger(), nullable=True),
        sa.Column("successful_domain_revision", sa.Integer(), nullable=True),
        sa.Column("success_outbox_event_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "session_id", "action", "request_hash", name="uq_domain_integration_request"
        ),
        sa.UniqueConstraint(
            "session_id", "action", "source_tool_call_id", name="uq_domain_integration_call"
        ),
        schema="app",
    )
    op.create_index(
        "ix_domain_integrations_entity",
        "domain_integrations",
        ["entity_kind", "entity_id"],
        schema="app",
    )
    op.create_index(
        "ix_domain_integrations_status",
        "domain_integrations",
        ["status"],
        schema="app",
    )

    op.create_table(
        "checkpoint_heads",
        sa.Column("thread_id", sa.Text(), primary_key=True),
        sa.Column("checkpoint_ns", sa.Text(), primary_key=True, server_default=""),
        sa.Column("accepted_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("writer_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("control_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("checkpoint_heads", schema="app")
    op.drop_index("ix_domain_integrations_status", table_name="domain_integrations", schema="app")
    op.drop_index("ix_domain_integrations_entity", table_name="domain_integrations", schema="app")
    op.drop_table("domain_integrations", schema="app")
    op.drop_index("ix_artifact_links_to", table_name="artifact_links", schema="app")
    op.drop_table("artifact_links", schema="app")
    op.drop_index("ix_artifact_provenance_export", table_name="artifact_provenance", schema="app")
    op.drop_index("ix_artifact_provenance_resource", table_name="artifact_provenance", schema="app")
    op.drop_index("ix_artifact_provenance_origin", table_name="artifact_provenance", schema="app")
    op.drop_table("artifact_provenance", schema="app")
    op.drop_index("ix_artifacts_resolution", table_name="artifacts", schema="app")
    op.drop_index("ix_artifacts_export_call", table_name="artifacts", schema="app")
    op.drop_column("artifacts", "resolution_status", schema="app")
    op.drop_column("artifacts", "source_trust", schema="app")
    op.drop_column("artifacts", "producer_kind", schema="app")
    op.drop_column("artifacts", "export_tool_call_id", schema="app")
