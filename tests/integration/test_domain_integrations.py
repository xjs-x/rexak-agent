"""P0：领域整合回执的幂等与冲突。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_workbench.domain.integrations import record_integration
from agent_workbench.persistence.tables import DomainIntegrationRow
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def _record(db, context, *, call_id: str, request_hash: str, status: str):
    return await record_integration(
        db,
        session_id=context.toolbox.session_id,
        run_id=context.toolbox.run_id,
        entity_kind="lead",
        entity_id=uuid4(),
        action="record_lead",
        source_tool_call_id=call_id,
        request_hash=request_hash,
        status=status,
        successful_domain_revision=2 if status == "integrated" else None,
        error_code=None if status == "integrated" else "validation_failed",
    )


async def test_integration_is_idempotent_by_request_and_conflicts_on_call_reuse(
    database, unique_name
):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        first = await _record(db, context, call_id="call-1", request_hash="a" * 64, status="rejected")
        again = await _record(db, context, call_id="call-2", request_hash="a" * 64, status="integrated")
        assert first.integration_id == again.integration_id
        assert again.status == "integrated"
        assert again.successful_domain_revision == 2

        with pytest.raises(ValueError):
            await _record(db, context, call_id="call-1", request_hash="b" * 64, status="rejected")

    async with database.session() as db:
        rows = (
            await db.execute(
                select(DomainIntegrationRow).where(
                    DomainIntegrationRow.session_id == context.toolbox.session_id
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "integrated"
