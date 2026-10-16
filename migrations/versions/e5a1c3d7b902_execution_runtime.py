"""命令执行底座（V4）：资源投影、wait-set、值班委托、事件接收、delivery、模板。

方案：`docs/plans/execution-runtime-implementation-spec.md` §12.2。

要点：
- 只增加**必要专用领域表**，复用已有 ToolOperationRow / ToolJobRow / EventRow / ArtifactRow；
- 资源表是控制端对 Executor 台账的只读投影，不承载真实执行状态；
- 图状态只引用 wait/delivery/delegation ID，不保存长日志或第二份可写资源状态；
- 迁移只建表，不改写既有运行数据。

Revision ID: e5a1c3d7b902
Revises: c9e40b17fa32
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "e5a1c3d7b902"
down_revision: str | None = "c9e40b17fa32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_resources",
        sa.Column("resource_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("executor_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="command"),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="starting"),
        sa.Column(
            "interaction_state", sa.String(length=32), nullable=False, server_default="not_applicable"
        ),
        sa.Column("origin_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("origin_operation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("termination_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "output_limit_action",
            sa.String(length=32),
            nullable=False,
            server_default="preserve_process",
        ),
        sa.Column("quota", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("close_state", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("close_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_status", sa.String(length=32), nullable=False, server_default="in_sync"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="app",
    )
    op.create_index(
        "ix_execution_resources_session",
        "execution_resources",
        ["session_id", "state"],
        schema="app",
    )
    op.create_index(
        "ix_execution_resources_run", "execution_resources", ["origin_run_id"], schema="app"
    )

    op.create_table(
        "execution_wait_sets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("resource_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("event_types", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("match", sa.String(length=8), nullable=False, server_default="any"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("creator_operation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("satisfied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="app",
    )
    op.create_index(
        "ix_execution_wait_sets_run", "execution_wait_sets", ["run_id", "status"], schema="app"
    )
    op.create_index(
        "ix_execution_wait_sets_session",
        "execution_wait_sets",
        ["session_id", "status"],
        schema="app",
    )

    op.create_table(
        "execution_delegations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("event_filters", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="once"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("authorization_source", sa.String(length=32), nullable=False, server_default="user_message"),
        sa.Column("authorization_command_id", UUID(as_uuid=True), nullable=True),
        sa.Column("authorization_quote", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="app",
    )
    op.create_index(
        "ix_execution_delegations_session",
        "execution_delegations",
        ["session_id", "status"],
        schema="app",
    )

    op.create_table(
        "execution_event_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("executor_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("availability", sa.String(length=32), nullable=False, server_default="available"),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_by_batch", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint(
            "executor_id", "event_id", name="uq_execution_event_receipts_origin"
        ),
        schema="app",
    )
    op.create_index(
        "ix_execution_event_receipts_session",
        "execution_event_receipts",
        ["session_id", "sequence"],
        schema="app",
    )
    op.create_index(
        "ix_execution_event_receipts_unconsumed",
        "execution_event_receipts",
        ["session_id", "consumed_by_batch"],
        schema="app",
    )

    op.create_table(
        "execution_event_cursors",
        sa.Column("executor_id", sa.String(length=128), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="app",
    )

    op.create_table(
        "execution_delivery_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("delegation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_receipt_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="prepared"),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("message_command_id", UUID(as_uuid=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_execution_delivery_batches_message"),
        schema="app",
    )
    op.create_index(
        "ix_execution_delivery_batches_session",
        "execution_delivery_batches",
        ["session_id", "status"],
        schema="app",
    )

    op.create_table(
        "execution_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="command"),
        sa.Column("latest_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "name", name="uq_execution_templates_session_name"),
        schema="app",
    )

    op.create_table(
        "execution_template_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.execution_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("spec", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("script_artifact_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "template_id", "revision", name="uq_execution_template_versions_rev"
        ),
        schema="app",
    )


def downgrade() -> None:
    # 回退只删除本次新增的**空表/投影表**；不触碰任何执行证据与运行数据。
    op.drop_table("execution_template_versions", schema="app")
    op.drop_table("execution_templates", schema="app")
    op.drop_table("execution_delivery_batches", schema="app")
    op.drop_table("execution_event_cursors", schema="app")
    op.drop_table("execution_event_receipts", schema="app")
    op.drop_table("execution_delegations", schema="app")
    op.drop_table("execution_wait_sets", schema="app")
    op.drop_table("execution_resources", schema="app")
