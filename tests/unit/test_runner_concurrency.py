"""独立会话与取消控制可并发，单个慢任务不能堵住其他会话。"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from agent_workbench.runtime.runner import AgentRunner


class _Database:
    closed = False

    async def start(self):
        pass

    async def close(self):
        self.closed = True


async def test_slow_cancellation_does_not_occupy_model_worker_slots(settings, monkeypatch):
    database = _Database()
    runner = AgentRunner(
        settings=settings.model_copy(
            update={
                "runner_max_concurrent_runs": 2,
                "job_wakeup_interval_seconds": 1e30,
            }
        ),
        database=cast(Any, database),
    )
    conversations = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
    cancel_id = uuid4()
    started = []
    cancellation_started = asyncio.Event()
    both_started = asyncio.Event()

    async def candidates(*, only_cancelling=False, **kwargs):
        return [SimpleNamespace(id=cancel_id)] if only_cancelling else conversations

    async def step(*, only_run_id=None):
        if only_run_id == cancel_id:
            cancellation_started.set()
            await asyncio.Event().wait()
        else:
            started.append(only_run_id)
            if len(started) == 2:
                both_started.set()
                runner.request_stop()
            await both_started.wait()

    monkeypatch.setattr(runner, "_candidate_runs", candidates)
    monkeypatch.setattr(runner, "step", step)

    async def pausing(_ids):
        return []

    monkeypatch.setattr(runner, "_pausing_runs", pausing)
    await asyncio.wait_for(runner.serve(poll_interval=0.01), timeout=3)
    assert cancellation_started.is_set()
    assert set(started) == {item.id for item in conversations}
    assert len(started) == 2
    assert database.closed
