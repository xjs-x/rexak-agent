"""退出 Coverage 维护后，浏览器范围拒绝与证据隔离仍然有效。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import JobState
from agent_workbench.contracts.tool import JobReceipt, JobSnapshot
from agent_workbench.persistence.tables import ArtifactRow, CoverageRow, ToolOperationRow
from agent_workbench.tools import browser
from tests.integration.test_tool_dispatch import _build_context

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("redirect", [False, True])
async def test_browser_scope_failure_preserves_operation_without_coverage(
    database, unique_name, monkeypatch, redirect
):
    job_id = uuid4()

    class LocalExecutor:
        download_artifact = AsyncMock(side_effect=AssertionError("越界正文不得下载"))

        async def submit(self, operation):
            self.operation_id = UUID(operation["operation_id"])
            return JobReceipt(
                job_id=job_id,
                operation_id=self.operation_id,
                state=JobState.SUCCEEDED,
                accepted_at=datetime.now(UTC),
            )

        async def get_job(self, _job_id):
            return JobSnapshot(
                job_id=job_id, operation_id=self.operation_id, kind="browser", state=JobState.SUCCEEDED
            )

    executor = LocalExecutor()
    context = await _build_context(database, unique_name, executor_stub=executor)
    payload = (
        {
            "ok": True,
            "result": {
                "final_url_out_of_scope": True,
                "final_url": "https://outside.invalid/",
                "out_of_scope_reason": "fixture",
                "artifact_manifest": {"page.txt": {"sha256": "0" * 64}},
            },
        }
        if redirect
        else {"ok": False, "error": {"code": "scope_violation", "message": "fixture"}}
    )
    monkeypatch.setattr(browser, "wait_or_accept", AsyncMock(return_value=None))
    monkeypatch.setattr(browser, "download_result_json", AsyncMock(return_value=payload))
    result = await browser.run_browser_action(
        context.toolbox,
        tool_call_id="scope-fixture",
        tool_name="browser_click",
        action="click",
        target="fixture",
    )
    assert result.status == "failed"
    executor.download_artifact.assert_not_awaited()
    async with database.session() as db:
        operation = await db.get(ToolOperationRow, result.operation_id)
        assert operation is not None and operation.status == "failed"
        for table in (CoverageRow, ArtifactRow):
            assert (
                list(
                    (
                        await db.execute(
                            select(table).where(table.session_id == context.toolbox.session_id)
                        )
                    ).scalars()
                )
                == []
            )
