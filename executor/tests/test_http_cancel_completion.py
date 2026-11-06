"""HTTP completion and unknown target delivery are independent facts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import workbench_executor.api as api


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "unknown"])
async def test_finished_http_cancel_confirms_stop_without_destroying_result(monkeypatch, status):
    record = SimpleNamespace(kind="http_request", state=status, finished_at="2026-11-06T00:00:00Z")
    ledger = SimpleNamespace(get=AsyncMock(return_value=record), update=AsyncMock())
    supervisor = SimpleNamespace(cancel=AsyncMock(side_effect=AssertionError("no subprocess")))
    state = SimpleNamespace(ledger=ledger, supervisor=supervisor, _tasks={})
    monkeypatch.setattr(api, "get_state", lambda: state)
    for request_id in ["one", "one", "two"]:
        result = await api.cancel_job("job", api.CancelRequest(request_id=request_id))
        assert result.process_confirmed_exited and result.state == status
    assert all("state" not in call.kwargs for call in ledger.update.call_args_list)


@pytest.mark.asyncio
async def test_active_http_cancel_awaits_coroutine_instead_of_missing_process(monkeypatch):
    task = asyncio.create_task(asyncio.sleep(100))
    record = SimpleNamespace(kind="http_request", state="running", finished_at=None)
    ledger = SimpleNamespace(get=AsyncMock(return_value=record), update=AsyncMock())
    state = SimpleNamespace(ledger=ledger, _tasks={"job": task}, emit=lambda *_args: None)
    monkeypatch.setattr(api, "get_state", lambda: state)
    result = await api.cancel_job("job", api.CancelRequest(request_id="cancel"))
    assert result.process_confirmed_exited and task.done()


@pytest.mark.asyncio
async def test_unfinished_unknown_http_cannot_be_declared_stopped(monkeypatch):
    record = SimpleNamespace(kind="http_request", state="unknown", finished_at=None)
    ledger = SimpleNamespace(get=AsyncMock(return_value=record), update=AsyncMock())
    state = SimpleNamespace(ledger=ledger, _tasks={}, emit=lambda *_args: None,
                            supervisor=SimpleNamespace(cancel=AsyncMock(return_value=(False, "unknown"))))
    monkeypatch.setattr(api, "get_state", lambda: state)
    result = await api.cancel_job("job", api.CancelRequest(request_id="cancel"))
    assert not result.process_confirmed_exited and result.state == "unknown"


@pytest.mark.asyncio
async def test_http_transport_completion_racing_cancel_retains_unknown_delivery(monkeypatch):
    task = asyncio.create_task(asyncio.sleep(100))
    running = SimpleNamespace(kind="http_request", state="running", finished_at=None)
    finished = SimpleNamespace(kind="http_request", state="unknown", finished_at="2026-11-06T00:00:00Z")
    ledger = SimpleNamespace(get=AsyncMock(side_effect=[running, finished]), update=AsyncMock())
    monkeypatch.setattr(api, "get_state", lambda: SimpleNamespace(
        ledger=ledger, _tasks={"job": task}, emit=lambda *_args: None))
    result = await api.cancel_job("job", api.CancelRequest(request_id="cancel"))
    assert result.process_confirmed_exited and result.state == "unknown"
