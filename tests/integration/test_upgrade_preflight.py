"""启动前只读核对工作库的真实 Alembic 版本。"""

from __future__ import annotations

import pytest

from agent_workbench.persistence.bootstrap import (
    assert_business_schema_current,
    expected_business_revisions,
)

pytestmark = pytest.mark.integration


async def test_working_database_matches_current_migration_head(database) -> None:
    async with database.session() as session:
        installed = await assert_business_schema_current(session)
    assert installed == expected_business_revisions()
