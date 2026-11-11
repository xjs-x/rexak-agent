"""定向运行时，常驻 Runner 不碰其他 run 的唤醒与驱动。"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from agent_workbench.runtime.runner import AgentRunner


class _Database:
    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_serve_limits_wakeup_and_step_to_selected_run(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    from agent_workbench.runtime import jobs

    target = uuid4()
    observed: dict[str, object] = {}
    runner = AgentRunner(settings=settings, database=_Database())  # type: ignore[arg-type]

    async def wake(_database, *, settings, run_ids=None):
        observed["wake_run_ids"] = run_ids
        return {"woken": []}

    async def step(*, only_run_id=None):
        observed["step_run_id"] = only_run_id
        runner.request_stop()

    monkeypatch.setattr(jobs, "probe_and_wake", wake)
    monkeypatch.setattr(runner, "step", step)
    await runner.serve(poll_interval=0, only_run_id=target)

    assert observed == {"wake_run_ids": [target], "step_run_id": target}


@pytest.mark.asyncio
async def test_isolated_runner_checks_own_waits_before_wakeup(monkeypatch, settings):
    from agent_workbench.runtime import execution_events, jobs

    target = uuid4()
    observed = []
    runner = AgentRunner(settings=settings, database=cast(Any, _Database()))
    async def scoped(_db, *, settings, run_id):
        observed.append(("waits", run_id))
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("global execution cycle must stay disabled")
    async def wake(_db, *, settings, run_ids):
        observed.append(("wake", run_ids))
        return {"woken": []}
    async def step(*, only_run_id):
        runner.request_stop()
    monkeypatch.setattr(execution_events, "targeted_execution_cycle", scoped)
    monkeypatch.setattr(execution_events, "execution_cycle", forbidden)
    monkeypatch.setattr(jobs, "probe_and_wake", wake)
    monkeypatch.setattr(runner, "step", step)
    await runner.serve(poll_interval=0, only_run_id=target, execution_cycle_enabled=False)
    assert observed == [("waits", target), ("wake", [target])]
