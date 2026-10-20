"""P0-D：accepted head 与 pending writes 的 fencing。"""

from __future__ import annotations

import asyncio
import operator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict, cast

import pytest
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy import select

from agent_workbench.domain.run_state import bump_control_version, claim_run
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    CheckpointHeadRow,
    CheckpointWriteReceiptRow,
    RunRow,
)
from agent_workbench.runtime.checkpoint_fencing import (
    CheckpointFenceError,
    FencedCheckpointSaver,
    StaleWriterError,
    _content_hash,
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


@asynccontextmanager
async def _real_saver(database, unique_name):
    context = await context_for(database, unique_name)
    saver = await _saver(context, database)
    async with open_checkpointer_connection(database.settings) as connection:
        inner = AsyncPostgresSaver(connection)
        cast(Any, saver)._inner = inner
        saver.serde = inner.serde
        config: Any = {"configurable": {"thread_id": context.thread_id, "checkpoint_ns": ""}}
        config = await saver.aput(config, empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {})
        yield saver, inner, config


def _fanout_writes() -> list[tuple[str, Any]]:
    return [
        ("review_cursor", None), ("last_review_turn", 0),
        ("last_review_source_signature", "fixture"),
        ("__pregel_tasks", Send("worker", {"label": "first"})),
        ("__pregel_tasks", Send("worker", {"label": "second"})),
    ]


async def test_real_saver_multi_send_same_channel_survives_retry_and_restart(database, unique_name):
    async with _real_saver(database, unique_name) as (saver, inner, config):
        writes = _fanout_writes()
        await asyncio.gather(*(saver.aput_writes(config, writes, "fanout") for _ in range(2)))
        # New wrapper instance reads persisted receipts, without process-local dedup state.
        restarted = FencedCheckpointSaver(inner, database,
            run_id=cast(Any, saver)._run_id, thread_id=cast(Any, saver)._thread_id,
            owner_generation=cast(Any, saver)._owner_generation,
            control_version=cast(Any, saver)._control_version, writer_epoch=0)
        result = await restarted.aget_tuple(config)
        assert result is not None and result.pending_writes is not None
        assert [(channel, value) for _, channel, value in result.pending_writes] == writes
        async with database.session() as db:
            rows = (await db.execute(select(CheckpointWriteReceiptRow).where(
                CheckpointWriteReceiptRow.thread_id == config["configurable"]["thread_id"],
            ))).scalars().all()
        assert len(rows) == len(writes)


async def test_slot_conflict_is_rejected_before_inner_saver_can_change_writes(database, unique_name):
    async with _real_saver(database, unique_name) as (saver, inner, config):
        await saver.aput_writes(config, [("first", 1)], "same-task")
        before = await inner.aget_tuple(config)
        with pytest.raises(CheckpointFenceError):
            await saver.aput_writes(config, [("different-channel", 2)], "same-task")
        after = await inner.aget_tuple(config)
        assert before is not None and after is not None
        assert before.pending_writes == after.pending_writes


@pytest.mark.parametrize("changed", [False, True])
async def test_inner_commit_without_receipts_reconciles_only_identical_retry(database, unique_name, changed):
    async with _real_saver(database, unique_name) as (saver, inner, config):
        writes = _fanout_writes()
        await inner.aput_writes(config, writes, "orphan-task")  # Crash after inner commit.
        untrusted = await saver.aget_tuple(config)
        assert untrusted is not None and untrusted.pending_writes == []
        if changed:
            with pytest.raises(CheckpointFenceError):
                await saver.aput_writes(config, [("changed", "unpersisted")], "orphan-task")
            result = await saver.aget_tuple(config)
            assert result is not None and result.pending_writes == []
        else:
            await saver.aput_writes(config, writes, "orphan-task")
            result = await saver.aget_tuple(config)
            assert result is not None and len(result.pending_writes or []) == len(writes)


async def test_reserved_write_updates_follow_official_saver_semantics(database, unique_name):
    async with _real_saver(database, unique_name) as (saver, _inner, config):
        await saver.aput_writes(config, [("__error__", "first error")], "failed-task")
        await saver.aput_writes(config, [("__error__", "latest error")], "failed-task")
        result = await saver.aget_tuple(config)
        assert result is not None and result.pending_writes == [("failed-task", "__error__", "latest error")]


async def test_receipt_transaction_abort_keeps_full_fanout_untrusted_until_retry(database, unique_name):
    class FailingDatabase:
        session = database.session

        @asynccontextmanager
        async def transaction(self):
            async with database.transaction() as db:
                yield db
                raise RuntimeError("injected receipt commit failure")

    async with _real_saver(database, unique_name) as (saver, _inner, config):
        cast(Any, saver)._db = FailingDatabase()
        with pytest.raises(RuntimeError, match="injected receipt commit failure"):
            await saver.aput_writes(config, _fanout_writes(), "aborted-task")
        cast(Any, saver)._db = database
        result = await saver.aget_tuple(config)
        assert result is not None and result.pending_writes == []
        await saver.aput_writes(config, _fanout_writes(), "aborted-task")
        result = await saver.aget_tuple(config)
        assert result is not None and len(result.pending_writes or []) == 5


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_legacy_receipts_require_unambiguous_channel_and_preserve_history(database, unique_name, ambiguous):
    async with _real_saver(database, unique_name) as (saver, inner, config):
        writes = [("channel", {"x": 1})]
        if ambiguous:
            writes.append(("channel", {"x": 2}))
        await inner.aput_writes(config, writes, "legacy-task")
        async with database.transaction() as db:
            db.add(CheckpointWriteReceiptRow(thread_id=config["configurable"]["thread_id"],
                checkpoint_ns="", checkpoint_id=config["configurable"]["checkpoint_id"], task_id="legacy-task",
                write_key="channel", content_hash=_content_hash({"x": 1}),
                writer_epoch=0, owner_generation=cast(Any, saver)._owner_generation,
                control_version=cast(Any, saver)._control_version))
        result = await saver.aget_tuple(config)
        assert result is not None
        assert result.pending_writes == ([] if ambiguous else [("legacy-task", "channel", {"x": 1})])
        async with database.session() as db:
            rows = (await db.execute(select(CheckpointWriteReceiptRow).where(
                CheckpointWriteReceiptRow.thread_id == config["configurable"]["thread_id"],
            ))).scalars().all()
        assert len(rows) == 1 and rows[0].write_key == "channel"  # No implicit backfill.


async def test_duplicate_identical_sends_need_distinct_receipts(database, unique_name):
    async with _real_saver(database, unique_name) as (saver, _inner, config):
        writes = [("__pregel_tasks", Send("worker", {"label": "same"}))] * 2
        await saver.aput_writes(config, writes, "duplicate-task")
        async with database.transaction() as db:
            row = (await db.execute(select(CheckpointWriteReceiptRow).where(
                CheckpointWriteReceiptRow.thread_id == config["configurable"]["thread_id"],
                CheckpointWriteReceiptRow.write_key == "v2:slot:0",
            ))).scalar_one()
            await db.delete(row)
        result = await saver.aget_tuple(config)
        assert result is not None and len(result.pending_writes or []) == 1


async def test_mixed_reserved_and_regular_writes_keep_official_indices(database, unique_name):
    async with _real_saver(database, unique_name) as (saver, _inner, config):
        writes = [("__error__", "error"), ("__pregel_tasks", Send("worker", {"label": "a"})),
                  ("__pregel_tasks", Send("worker", {"label": "b"}))]
        await saver.aput_writes(config, writes, "mixed-task")
        result = await saver.aget_tuple(config)
        assert result is not None
        assert [(channel, value) for _, channel, value in (result.pending_writes or [])] == writes
        async with database.session() as db:
            rows = (await db.execute(select(CheckpointWriteReceiptRow).where(
                CheckpointWriteReceiptRow.thread_id == config["configurable"]["thread_id"],
            ))).scalars().all()
        assert {row.write_key for row in rows} == {"v2:slot:-1", "v2:slot:1", "v2:slot:2"}


async def test_multi_send_write_still_rejected_after_pause(database, unique_name):
    async with _real_saver(database, unique_name) as (saver, inner, config):
        async with database.transaction() as db:
            await bump_control_version(db, run_id=cast(Any, saver)._run_id)
        with pytest.raises(StaleWriterError):
            await saver.aput_writes(config, _fanout_writes(), "late-fanout")
        result = await inner.aget_tuple(config)
        assert result is not None and result.pending_writes == []


async def test_real_graph_dispatches_two_sends_and_checkpoint_restores(database, unique_name):
    class FanoutState(TypedDict):
        results: Annotated[list[str], operator.add]
        label: str

    async with _real_saver(database, unique_name) as (saver, _inner, config):
        def dispatch(state: Any) -> dict[str, Any]:
            return {}

        def worker(state: Any) -> dict[str, Any]:
            return {"results": [state["label"]]}

        graph = StateGraph(FanoutState)
        graph.add_node("dispatch", dispatch)
        graph.add_node("worker", worker)
        graph.add_edge(START, "dispatch")
        graph.add_conditional_edges("dispatch", lambda _state: [
            Send("worker", {"label": "a"}), Send("worker", {"label": "b"}),
        ])
        graph.add_edge("worker", END)
        compiled = graph.compile(checkpointer=saver)
        result = await compiled.ainvoke(cast(Any, {"results": [], "label": "root"}), config)
        assert sorted(result["results"]) == ["a", "b"]
        restored = await graph.compile(checkpointer=saver).aget_state(
            {"configurable": {"thread_id": config["configurable"]["thread_id"]}},
        )
        assert sorted(restored.values["results"]) == ["a", "b"] and restored.next == ()
