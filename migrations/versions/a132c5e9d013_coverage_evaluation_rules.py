"""冻结 profile 的裁决定义；不改写历史覆盖与 checkpoint。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a132c5e9d013"
down_revision = "b2d4e5f6a7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("coverage_profiles", sa.Column("evaluation_rules", JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb")), schema="app")
    op.add_column("coverage_profiles", sa.Column("basis_versions", JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb")), schema="app")


def downgrade() -> None:
    raise RuntimeError("裁决定义用于历史审计，禁止通过 downgrade 删除")
