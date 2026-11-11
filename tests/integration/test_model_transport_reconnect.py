"""Observed transport reconnection never invokes a graph or tool twice."""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2 as httpx
import pytest
from langchain_core.messages import AIMessage
from openai import APIConnectionError, APIStatusError
from sqlalchemy import select

from agent_workbench.agent.signals import GraphRunAborted
from agent_workbench.models import invocation
from agent_workbench.models.invocation import InvocationScope, ModelRequestTimeout, invoke_attempt
from agent_workbench.persistence.tables import EventRow, ModelCallRow
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("error", ["connection", "timeout", "429", "503"])
async def test_transient_errors_retry_with_same_logical_id_and_unknown_usage(database, unique_name, monkeypatch, error):
    context = await context_for(database, unique_name)
    box = context.toolbox
    box.settings.model_auto_reconnect = True
    box.assert_control_current = AsyncMock()
    pause = AsyncMock()
    monkeypatch.setattr(invocation, "_reconnect_pause", pause)
    request = httpx.Request("POST", "https://example.invalid")
    exc = (APIConnectionError(request=request) if error == "connection" else
           ModelRequestTimeout("fixture") if error == "timeout" else
           APIStatusError("fixture", response=httpx.Response(int(error), request=request,
                          headers={"Retry-After": "7"}), body=None))
    answer = AIMessage(content="done")
    call = AsyncMock(side_effect=[exc, exc, answer])
    logical_id = uuid4()
    assert await invoke_attempt(call, scope=InvocationScope(box), logical_id=logical_id, phase="agent") is answer
    async with database.session() as db:
        rows = (await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == box.run_id).order_by(ModelCallRow.retry_index))).scalars().all()
        events = (await db.execute(select(EventRow).where(EventRow.run_id == box.run_id))).scalars().all()
    assert len(rows) == 3 and {r.logical_call_id for r in rows} == {logical_id}
    assert [r.retry_index for r in rows] == [0, 1, 2]
    assert all(r.usage_status == "unknown" for r in rows[:2])
    assert len({r.model_call_id for r in rows}) == 3
    assert sum(e.payload.get("phase") == "retry_wait" for e in events) == 2
    if error in {"429", "503"}:
        assert all(c.args[1] >= 7 for c in pause.call_args_list)


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_permanent_http_errors_do_not_retry(database, unique_name, status):
    context = await context_for(database, unique_name)
    context.toolbox.settings.model_auto_reconnect = True
    request = httpx.Request("POST", "https://example.invalid")
    call = AsyncMock(side_effect=APIStatusError("fixture", response=httpx.Response(status, request=request), body=None))
    with pytest.raises(APIStatusError):
        await invoke_attempt(call, scope=InvocationScope(context.toolbox), logical_id=uuid4(), phase="agent")
    assert call.await_count == 1


async def test_control_change_during_retry_prevents_second_request(database, unique_name, monkeypatch):
    context = await context_for(database, unique_name)
    box = context.toolbox
    box.settings.model_auto_reconnect = True
    box.assert_control_current = AsyncMock()
    async def stop(_box, _delay):
        raise GraphRunAborted("fixture cancellation")
    monkeypatch.setattr(invocation, "_reconnect_pause", stop)
    call = AsyncMock(side_effect=APIConnectionError(request=httpx.Request("POST", "https://example.invalid")))
    with pytest.raises(GraphRunAborted):
        await invoke_attempt(call, scope=InvocationScope(box), logical_id=uuid4(), phase="agent")
    assert call.await_count == 1


async def test_reconnect_pause_is_interruptible():
    from types import SimpleNamespace
    box = SimpleNamespace(assert_control_current=AsyncMock())
    task = asyncio.create_task(invocation._reconnect_pause(box, 60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_explicit_rejection_is_not_retried_even_with_503(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.settings.model_auto_reconnect = True
    request = httpx.Request("POST", "https://example.invalid")
    error = APIStatusError("fixture", response=httpx.Response(503, request=request),
        body={"error": {"code": "data_inspection_failed"}})
    call = AsyncMock(side_effect=error)
    with pytest.raises(APIStatusError):
        await invoke_attempt(call, scope=InvocationScope(context.toolbox), logical_id=uuid4(), phase="agent")
    assert call.await_count == 1
