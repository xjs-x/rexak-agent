"""退出宿主方法的 Skill 转换项，保留历史版本和运行证据。"""

from alembic import op

revision = "a8b3d5f7c012"
down_revision = "f7a2c4e6b901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE app.skill_installations SET enabled = false, project_ids = '[]'::jsonb, "
               "default_for_projects = '[]'::jsonb WHERE source LIKE 'builtin:%'")


def downgrade() -> None:
    # 不自动重新启用错误转换项，避免反向迁移恢复默认注入。
    pass
