"""弱点—关系—计划—执行在同一条链上的工具行为。"""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest
from langchain.tools import ToolRuntime

from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def _call(
    tool_obj: Any,
    context: Any,
    args: dict[str, Any],
    call_id: str,
    state: dict[str, Any] | None = None,
) -> Any:
    runtime = ToolRuntime(
        state=state or {},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=call_id,
        store=None,
    )
    function = cast(Any, tool_obj).coroutine
    parameters = inspect.signature(function).parameters
    if "tool_call_id" in parameters:
        args = {**args, "tool_call_id": call_id}
    return await function(runtime, **args)


async def test_weakness_scan_and_review_are_not_truncated_by_display_limits(
    database, unique_name
):
    """展示有上限，但**结束复查集合**必须完整（超过展示上限也要发现）。"""
    from agent_workbench.agent.working_context import assemble_working_context
    from agent_workbench.domain.findings import FindingService
    from agent_workbench.runtime.stopping import FinishGate

    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    async with database.transaction() as db:
        service = FindingService(db)
        for index in range(210):
            await service.upsert_weakness(session_id=session_id, title=f"弱点 {index}")
    async with database.session() as db:
        view = await assemble_working_context(db, session_id, {"objective": "上限验收"})
        gate = await FinishGate(db).evaluate(
            session_id=session_id,
            proposal={
                "reason": "goal_answered",
                "goal_answer": "完成",
                "stop_rationale": "没有方向",
            },
        )
    assert view["weakness_consumption"]["pending_connection_review_count"] == 210
    assert gate.allowed and gate.unreviewed_count == 0, "历史库存不参与最小结束门禁"
