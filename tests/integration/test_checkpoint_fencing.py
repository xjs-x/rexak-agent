"""P0-D：accepted head 与 pending writes 的 fencing。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from langgraph.checkpoint.base import CheckpointTuple
from sqlalchemy import select

from agent_workbench.domain.run_state import bump_control_version, claim_run
from agent_workbench.persistence.tables import (
    CheckpointHeadRow,
    CheckpointWriteReceiptRow,
    RunRow,
)
from agent_workbench.runtime.checkpoint_fencing import (
    CheckpointFenceError,
    FencedCheckpointSaver,
    StaleWriterError,
)
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("method", ["aput", "aput_writes"])
@pytest.mark.parametrize("invalid", ["released", "missing_lease", "expired"])
async def test_all_write_paths_require_current_owner_and_live_lease(
    database, unique_name, method, invalid
):
    from agent_workbench.domain.run_state import release_run

    context = await context_for(database, unique_name)
    saver = await _saver(context, database)
    async with database.transaction() as db:
        row = await db.get(RunRow, context.toolbox.run_id)
        assert row is not None
        if invalid == "released":
            await release_run(db, run_id=row.id, owner="test-writer")
        elif invalid == "missing_lease":
            row.lease_until = None
        else:
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    config: Any = {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}}
    with pytest.raises(StaleWriterError):
        if method == "aput":
            await saver.aput(config, _empty(), _empty(), _empty())
        else:
            await saver.aput_writes(config, [("channel", "late")], "late-task")
    assert not cast(Any, saver)._inner.saved and not cast(Any, saver)._inner.writes


def _empty() -> Any:
    return {}


class _FakeSaver:
    def __init__(self) -> None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        self.serde = JsonPlusSerializer()
        self.config_specs: list[Any] = []
        self.saved: list[dict[str, Any]] = []
        self.writes: list[tuple[str, Any]] = []
        self.last_read: dict[str, Any] | None = None
        self._checkpoint_id = "ckpt-1"

    async def aput(self, config, checkpoint, metadata, new_versions):
        self.saved.append({"config": config, "checkpoint": checkpoint})
        return {
            **config,
            "configurable": {
                **config.get("configurable", {}),
                "checkpoint_id": self._checkpoint_id,
            },
        }

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self.writes.extend(writes)

    async def aget_tuple(self, config):
        self.last_read = config
        return

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if False:
            yield None

    def get_next_version(self, current, channel):
        return "1"

    async def adelete_thread(self, thread_id):
        return None


async def _saver(context, database, *, writer_epoch=0, control_version=None):
    async with database.transaction() as db:
        acquired, generation = await claim_run(
            db, run_id=context.toolbox.run_id, owner="test-writer", lease_seconds=300
        )
        assert acquired
    async with database.session() as db:
        run = await db.get(RunRow, context.toolbox.run_id)
        assert run is not None
        version = int(run.control_version if control_version is None else control_version)
        return FencedCheckpointSaver(
            cast(Any, _FakeSaver()),
            database,
            run_id=run.id,
            thread_id=context.thread_id,
            owner_generation=int(generation),
            control_version=version,
            writer_epoch=writer_epoch,
        )


async def test_aput_advances_accepted_head_and_recovery_reads_it(database, unique_name):
    context = await context_for(database, unique_name)
    saver = await _saver(context, database)
    config: Any = {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}}
    saved: Any = await saver.aput(config, _empty(), _empty(), _empty())
    assert saved["configurable"]["checkpoint_id"] == "ckpt-1"
    async with database.session() as db:
        head = await db.get(CheckpointHeadRow, (context.thread_id, ""))
    assert head is not None
    assert head.accepted_checkpoint_id == "ckpt-1"

    await saver.aget_tuple(config)
    inner = cast(Any, saver)._inner
    assert inner.last_read["configurable"]["checkpoint_id"] == "ckpt-1"


async def test_pause_fences_old_writer_for_both_write_paths(database, unique_name):
    context = await context_for(database, unique_name)
    saver = await _saver(context, database)
    config: Any = {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}}
    await saver.aput(config, _empty(), _empty(), _empty())

    async with database.transaction() as db:
        await bump_control_version(db, run_id=context.toolbox.run_id)

    with pytest.raises(StaleWriterError):
        await saver.aput(config, _empty(), _empty(), _empty())
    with pytest.raises(StaleWriterError):
        await saver.aput_writes(config, [("channel", {"x": 1})], "task-1")

    async with database.session() as db:
        run = await db.get(RunRow, context.toolbox.run_id)
        head = await db.get(CheckpointHeadRow, (context.thread_id, ""))
    assert run is not None and head is not None
    fresh = await _saver(
        context,
        database,
        writer_epoch=int(head.writer_epoch),
        control_version=int(run.control_version),
    )
    await fresh.aput(config, _empty(), _empty(), _empty())
    assert (await fresh.aget_tuple(config)) is None


class _PendingSaver(_FakeSaver):
    async def aget_tuple(self, config) -> Any:
        return CheckpointTuple(
            config=cast(
                Any,
                {
                    **config,
                    "configurable": {
                        **config.get("configurable", {}),
                        "checkpoint_id": "ckpt-1",
                    },
                },
            ),
            checkpoint=cast(Any, {"id": "ckpt-1"}),
            metadata=cast(Any, {}),
            parent_config=None,
            pending_writes=[("task-1", "channel", {"x": 1})],
        )


async def test_unreceipted_pending_write_is_not_recovered(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        acquired, generation = await claim_run(
            db, run_id=context.toolbox.run_id, owner="test-writer", lease_seconds=300
        )
        assert acquired
    async with database.session() as db:
        run = await db.get(RunRow, context.toolbox.run_id)
        assert run is not None
        saver = FencedCheckpointSaver(
            cast(Any, _PendingSaver()),
            database,
            run_id=run.id,
            thread_id=context.thread_id,
            owner_generation=int(generation),
            control_version=int(run.control_version),
            writer_epoch=0,
        )
    config: Any = {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}}
    await saver.aput(config, _empty(), _empty(), _empty())
    assert (await saver.aget_tuple(config)) is not None
    recovered = await saver.aget_tuple(config)
    assert recovered is not None and recovered.pending_writes == []

    await saver.aput_writes(config, [("channel", {"x": 1})], "task-1")
    recovered = await saver.aget_tuple(config)
    assert recovered is not None
    assert recovered.pending_writes is not None
    assert len(recovered.pending_writes) == 1


async def test_pending_write_receipts_are_idempotent_and_conflict_on_content(database, unique_name):
    context = await context_for(database, unique_name)
    saver = await _saver(context, database)
    config: Any = {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}}
    await saver.aput(config, _empty(), _empty(), _empty())
    await saver.aput_writes(config, [("channel", {"x": 1})], "task-1")
    await saver.aput_writes(config, [("channel", {"x": 1})], "task-1")
    async with database.session() as db:
        receipts = (
            (
                await db.execute(
                    select(CheckpointWriteReceiptRow).where(
                        CheckpointWriteReceiptRow.thread_id == context.thread_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(receipts) == 1
    with pytest.raises(CheckpointFenceError):
        await saver.aput_writes(config, [("channel", {"x": 2})], "task-1")
