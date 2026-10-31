"""独立聊天与项目归类；保留所有既有数据并回填拥有者。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from agent_workbench.config.settings import get_settings

revision = "e82d934a10bc"
down_revision = ("b903eaf18201", "d240a68c917e")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("owner_id", sa.String(128)), schema="app")
    op.execute("UPDATE app.sessions s SET owner_id=p.owner_id FROM app.projects p WHERE p.id=s.project_id")
    op.alter_column("sessions", "owner_id", nullable=False,
                    server_default=sa.text("'" + get_settings().owner_id.replace("'", "''") + "'"), schema="app")
    op.alter_column("sessions", "project_id", nullable=True, schema="app")
    op.alter_column("identities", "project_id", nullable=True, schema="app")
    op.add_column("identities", sa.Column("session_id", UUID(as_uuid=True)), schema="app")
    op.create_foreign_key("fk_identities_session_id_sessions", "identities", "sessions",
                          ["session_id"], ["id"], source_schema="app", referent_schema="app",
                          ondelete="CASCADE")
    op.drop_constraint("uq_identities_project_name", "identities", schema="app", type_="unique")
    op.create_index("uq_identities_project_name", "identities", ["project_id", "display_name"],
                    schema="app", unique=True, postgresql_where=sa.text("session_id IS NULL"))
    op.create_unique_constraint("uq_identities_session_name", "identities",
                                ["session_id", "display_name"], schema="app")


def downgrade() -> None:
    # 无损降级不可能把独立聊天强行塞入项目，拒绝隐式搬迁或删除。
    raise RuntimeError("此迁移需显式制定无损数据映射后方可降级")
