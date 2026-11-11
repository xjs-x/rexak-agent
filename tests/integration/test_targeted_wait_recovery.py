"""A private assessment runner settles only its own waits and retains final END."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.run_state import set_run_status
from agent_workbench.persistence.repositories import (
    ExecutionEventCursorRepository,
    ExecutionResourceRepository,
    ExecutionWaitSetRepository,
)
from agent_workbench.persistence.tables import (
    CommandRow,
    ExecutionEventReceiptRow,
    ExecutionResourceRow,
    ExecutionWaitSetRow,
    RunRow,
)
from agent_workbench.runtime import execution_events
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor, FakeResource

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("final_saved", [True, False])
async def test_targeted_cycle_settles_finished_jobs_without_touching_foreign_run(database, unique_name, monkeypatch, final_saved):
    with TestClient(create_app()) as client:
        sid, rid, _ = create_task(client, unique_name)
        foreign_sid, foreign_rid, _ = create_task(client, unique_name + "-foreign")
    fake = FakeExecutor()
    ids = [uuid4(), uuid4()]
    async with database.transaction() as db:
        for index, (session_id, run_id) in enumerate([(sid, rid), (foreign_sid, foreign_rid)]):
            await set_run_status(db, run_id=run_id, target=RunStatus.RUNNING)
            await set_run_status(db, run_id=run_id, target=RunStatus.WAITING_TOOL)
            await ExecutionResourceRepository(db).upsert(resource_id=ids[index], session_id=session_id,
                origin_run_id=run_id, executor_id="kali-01", kind="command", state="running")
            await ExecutionWaitSetRepository(db).create(session_id=session_id, run_id=run_id,
                resource_ids=[str(ids[index])], event_types=[], match="all")
            fake.resources[str(ids[index])] = FakeResource(str(ids[index]), "command", state="exited", exit_code=0)
        if final_saved:
            run = await db.get(RunRow, rid)
            run.config_snapshot = {"completion_result_artifact_id": str(uuid4())}
    class Client:
        async def start(self): pass
        async def close(self): pass
        async def execution_resource(self, resource_id):
            assert resource_id == str(ids[0])
            return await fake.execution_resource(resource_id)
    monkeypatch.setattr(execution_events, "HttpExecutorClient", lambda *_a, **_k: Client())
    monkeypatch.setattr(execution_events, "SshTunnel", lambda *_a: SimpleNamespace(close=lambda: None))
    await execution_events.targeted_execution_cycle(database, settings=database.settings, run_id=rid)
    async with database.session() as db:
        assert (await db.get(RunRow, rid)).status == "queued"
        assert (await db.get(RunRow, foreign_rid)).status == "waiting_tool"
        assert (await db.get(ExecutionResourceRow, ids[1])).state == "running"
        waits = (await db.execute(select(ExecutionWaitSetRow).where(ExecutionWaitSetRow.run_id == foreign_rid))).scalars().all()
        assert all(w.status == "open" for w in waits)
        commands = (await db.execute(select(CommandRow).where(CommandRow.run_id == rid))).scalars().all()
        wake_commands = [c for c in commands if (c.payload or {}).get("source") == "job_wakeup"]
        assert len(wake_commands) == (0 if final_saved else 1)


async def test_scoped_event_ingestion_keeps_global_cursor_and_foreign_events_untouched(database, unique_name):
    with TestClient(create_app()) as client:
        sid, rid, _ = create_task(client, unique_name)
        other_sid, _, _ = create_task(client, unique_name + "-other")
    resource_id = uuid4()
    cfg = database.settings.model_copy(update={"executor_id": f"scope-{uuid4()}"})
    async with database.transaction() as db:
        await ExecutionResourceRepository(db).upsert(resource_id=resource_id, session_id=sid,
            origin_run_id=rid, executor_id="kali-01", kind="command", state="running")
        await ExecutionEventCursorRepository(db).put(cfg.executor_id, 1)
    fake = FakeExecutor()
    fake.events = [{"seq": i, "event_id": str(uuid4()), "executor_id": "kali-01",
        "session_id": str(owner), "resource_id": str(resource_id), "type": "resource.exited",
        "occurred_at": datetime.now(UTC).isoformat(), "payload": {}}
        for i, owner in [(1, other_sid), (2, sid)]]
    result = await execution_events.pull_events(database, settings=cfg, client=fake, run_id=rid)
    assert result["created"] == 1
    async with database.session() as db:
        assert await ExecutionEventCursorRepository(db).get(cfg.executor_id) == 1
        assert await ExecutionEventCursorRepository(db).get(f"targeted:{rid}") == 2
        assert not (await db.execute(select(ExecutionEventReceiptRow).where(ExecutionEventReceiptRow.session_id == other_sid))).scalars().all()



async def test_delayed_old_event_does_not_satisfy_new_wait(database, unique_name):
    from datetime import timedelta

    from agent_workbench.persistence.repositories import ExecutionEventReceiptRepository

    with TestClient(create_app()) as client:
        sid, rid, _ = create_task(client, unique_name)
    resource_id = uuid4()
    fake = FakeExecutor()
    fake.resources[str(resource_id)] = FakeResource(str(resource_id), "command", state="running")
    async with database.transaction() as db:
        await set_run_status(db, run_id=rid, target=RunStatus.RUNNING)
        await set_run_status(db, run_id=rid, target=RunStatus.WAITING_TOOL)
        wait = await ExecutionWaitSetRepository(db).create(session_id=sid, run_id=rid,
            resource_ids=[str(resource_id)], event_types=["resource.watch_match"], match="any")
        await ExecutionEventReceiptRepository(db).insert_if_new(executor_id="kali-01",
            event_id=str(uuid4()), session_id=sid, resource_id=str(resource_id), operation_id=None,
            type="resource.watch_match", sequence=9001, availability="available", payload={},
            occurred_at=datetime.now(UTC) - timedelta(hours=1))
    result = await execution_events.evaluate_wait_sets(database, settings=database.settings, client=fake, run_ids=[rid])
    assert not result["satisfied"]
    async with database.transaction() as db:
        assert (await db.get(ExecutionWaitSetRow, wait.id)).status == "open"
        await ExecutionEventReceiptRepository(db).insert_if_new(executor_id="kali-01",
            event_id=str(uuid4()), session_id=sid, resource_id=str(resource_id), operation_id=None,
            type="resource.watch_match", sequence=9002, availability="available", payload={},
            occurred_at=datetime.now(UTC))
    result = await execution_events.evaluate_wait_sets(database, settings=database.settings, client=fake, run_ids=[rid])
    assert result["satisfied"]
