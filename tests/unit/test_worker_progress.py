"""Bounded Worker progress projection with a read-only fake database."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_workbench.domain.worker_progress import worker_progress


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def mappings(self):
        return self


class Database:
    def __init__(self, events):
        self.events = events
        self.queries = []
        self.calls = 0

    async def scalars(self, _query):
        self.calls += 1
        return Result([])

    async def scalar(self, _query):
        return None

    async def execute(self, query, params):
        self.queries.append(params)
        if "ORDER BY sequence DESC" in str(query):
            return Result([(max((event["sequence"] for event in self.events), default=0),)] if self.events else [])
        return Result([event for event in self.events if event["sequence"] > params["cursor"]][:params["limit"]])


@pytest.mark.asyncio
async def test_default_and_paged_history_are_bounded_and_scoped():
    delegation = SimpleNamespace(id=uuid4(), parent_run_id=uuid4(), parent_session_id=uuid4(),
        objective="inspect fixture", state="working", created_at=datetime.now(UTC))
    events = [{"sequence": index, "role": "assistant", "message": {"content": "x" * 10000},
               "created_at": datetime.now(UTC)} for index in range(1, 13)]
    db = Database(events)
    overview = await worker_progress(db, delegation)
    assert overview["recent_operations"] == overview["recent_artifacts"] == []
    assert overview["visible_history"]["has_more"] is True
    assert "events" not in overview["visible_history"]
    first = await worker_progress(db, delegation, cursor=0, limit=5)
    assert len(first["visible_history"]["events"]) == 5
    assert first["visible_history"]["next_cursor"] == 5
    assert first["visible_history"]["has_more"] is True
    assert first["visible_history"]["events"][0]["message"]["truncated"] is True
    assert all(params["id"] == delegation.id for params in db.queries)
    last = await worker_progress(db, delegation, cursor=10, limit=5)
    assert len(last["visible_history"]["events"]) == 2
    assert last["visible_history"]["has_more"] is False


@pytest.mark.asyncio
async def test_failed_worker_with_no_history_returns_empty():
    delegation = SimpleNamespace(id=uuid4(), parent_run_id=uuid4(), parent_session_id=uuid4(),
        objective="fixture", state="failed", created_at=datetime.now(UTC))
    result = await worker_progress(Database([]), delegation, cursor=0)
    assert result["state"] == "failed"
    assert result["visible_history"]["events"] == []
