"""P8 数据库失联注入：Runner 不得凭空推进或宣称 run 失败。"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.run_state import create_run
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import RunRow, ToolOperationRow
from agent_workbench.runtime.runner import AgentRunner

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


async def test_unavailable_database_leaves_run_unchanged(database, unique_name) -> None:
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="db-outage-test",
            name=unique_name,
            target_description="数据库失联演练",
            default_scope={},
        )
        task = await SessionRepository(session).create(
            project_id=project.id,
            title="数据库失联",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = await create_run(
            session,
            session_id=task.id,
            objective="不要在数据库失联时运行",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )

    url = make_url(get_settings().database_url).set(port=1).update_query_dict(
        {"connect_timeout": "1"}
    )
    settings = get_settings().model_copy(
        update={"database_url": url.render_as_string(hide_password=False)}
    )
    unavailable = Database(settings)
    await unavailable.start()
    try:
        with pytest.raises(OperationalError):
            await AgentRunner(settings=settings, database=unavailable).step(only_run_id=run.id)
    finally:
        await unavailable.close()

    async with database.session() as session:
        current = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        operations = (
            await session.execute(select(ToolOperationRow).where(ToolOperationRow.run_id == run.id))
        ).scalars().all()
    assert current.status == str(RunStatus.QUEUED)
    assert current.owner is None
    assert operations == []
