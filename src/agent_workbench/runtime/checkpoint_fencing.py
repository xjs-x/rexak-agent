"""checkpoint 写入隔离：accepted head + pending write receipts。

设计边界：
- 官方 `AsyncPostgresSaver` 的内部保存语句没有 writer epoch 条件，不能直接改；
- 本包装器把“接受点”放在 `checkpoint_heads`：只有 fence 通过且保存成功后，
  才推进 accepted head；崩溃留下的孤儿 checkpoint 不参与恢复；
- `aput_writes` 另写旁路收据，旧 epoch 的 pending writes 不进入调度器；
- 行锁只覆盖短数据库操作，不等待模型、网络工具或取消完成。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import bindparam, select, text

from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import (
    CheckpointHeadRow,
    CheckpointWriteReceiptRow,
    RunRow,
    SessionRow,
)


class StaleWriterError(RuntimeError):
    """writer epoch / owner generation / control version 已过期。"""


class CheckpointFenceError(RuntimeError):
    """pending writes 同键异内容或 head 状态非法。"""


def _content_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _slot_key(index: int) -> str:
    # Existing channel-keyed receipts remain readable; new keys identify framework slots.
    return f"v2:slot:{index}"


class FencedCheckpointSaver(BaseCheckpointSaver):
    """包装官方 Saver；恢复只读 accepted head。"""

    def __init__(
        self,
        inner: BaseCheckpointSaver,
        database: Database,
        *,
        run_id: UUID,
        thread_id: str,
        checkpoint_ns: str = "",
        owner_generation: int,
        control_version: int,
        writer_epoch: int,
        expected_checkpoint_id: str | None = None,
        writer_owner: str | None = None,
    ) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner
        self._db = database
        # Pin the schema of the connection; transaction decorators need not expose settings.
        self._checkpoint_schema = database.settings.checkpoint_schema
        self._run_id = run_id
        self._thread_id = thread_id
        self._checkpoint_ns = checkpoint_ns
        self._owner_generation = int(owner_generation)
        self._control_version = int(control_version)
        self._writer_epoch = int(writer_epoch)
        self._expected_checkpoint_id = expected_checkpoint_id
        self._writer_owner = writer_owner

    @property
    def config_specs(self):  # type: ignore[no-untyped-def]
        return self._inner.config_specs

    async def _lock_head(self, session: Any) -> CheckpointHeadRow:
        run = await session.get(RunRow, self._run_id, with_for_update=True)
        if run is None:
            raise StaleWriterError("run 不存在，拒绝 checkpoint 写入")
        if (
            int(run.owner_generation) != self._owner_generation
            or int(run.control_version) != self._control_version
        ):
            raise StaleWriterError("run 所有权或控制版本已变化，拒绝旧 writer 写入")
        if self._writer_owner is not None and run.owner != self._writer_owner:
            raise StaleWriterError("迁移/写入 owner 已变化，拒绝旧 writer 写入")
        if run.owner is None or run.lease_until is None:
            raise StaleWriterError("writer 无有效 owner 或租约，拒绝 checkpoint 写入")
        if run.lease_until <= datetime.now(UTC):
            raise StaleWriterError("租约已过期，拒绝旧 writer 写入")

        head = (
            await session.execute(
                select(CheckpointHeadRow)
                .where(
                    CheckpointHeadRow.thread_id == self._thread_id,
                    CheckpointHeadRow.checkpoint_ns == self._checkpoint_ns,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if head is None:
            # 新 thread 的受控 bootstrap：先建立空 head，保存成功后再推进 acceptance。
            head = CheckpointHeadRow(
                thread_id=self._thread_id,
                checkpoint_ns=self._checkpoint_ns,
                accepted_checkpoint_id=self._expected_checkpoint_id or "",
                writer_epoch=self._writer_epoch,
                owner_generation=self._owner_generation,
                control_version=self._control_version,
                accepted_at=datetime.now(UTC),
            )
            session.add(head)
            await session.flush()
        elif int(head.writer_epoch) != self._writer_epoch:
            raise StaleWriterError("writer epoch 已过期，拒绝旧 writer 写入")
        if (
            self._expected_checkpoint_id is not None
            and head.accepted_checkpoint_id != self._expected_checkpoint_id
        ):
            raise StaleWriterError("accepted head 已变化，拒绝基于旧基线的提交")
        return head

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async with self._db.transaction() as session:
            head = await self._lock_head(session)
            saved = await self._inner.aput(config, checkpoint, metadata, new_versions)
            checkpoint_id = saved.get("configurable", {}).get("checkpoint_id")
            if not checkpoint_id:
                raise CheckpointFenceError("Saver 未返回 checkpoint_id，不能推进 accepted head")
            head.accepted_checkpoint_id = str(checkpoint_id)
            head.writer_epoch = self._writer_epoch
            head.owner_generation = self._owner_generation
            head.control_version = self._control_version
            head.accepted_at = datetime.now(UTC)
        return saved

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async with self._db.transaction() as session:
            head = await self._lock_head(session)
            run = await session.get(RunRow, self._run_id, with_for_update=True)
            if run is None or run.owner is None:
                raise StaleWriterError("pending writes 需要有效 writer owner，拒绝无主写入")
            checkpoint_id = str(
                config.get("configurable", {}).get("checkpoint_id")
                or head.accepted_checkpoint_id
                or ""
            )
            if not checkpoint_id:
                raise CheckpointFenceError("pending writes 缺少 checkpoint_id，且没有 accepted head")
            write_config: Any = {
                **config,
                "configurable": {
                    **dict(config.get("configurable") or {}),
                    "checkpoint_id": checkpoint_id,
                },
            }
            configurable = write_config["configurable"]
            if (configurable.get("thread_id") != self._thread_id
                    or configurable.get("checkpoint_ns", "") != self._checkpoint_ns):
                raise CheckpointFenceError("pending writes 超出当前 thread/namespace")
            # Mirror official WRITES_IDX_MAP and batch INSERT/UPSERT selection exactly.
            upsert = all(channel in WRITES_IDX_MAP for channel, _ in writes)
            slots: dict[int, tuple[str, Any]] = {}
            for index, (channel, value) in enumerate(writes):
                slot = WRITES_IDX_MAP.get(channel, index)
                if upsert or slot not in slots:
                    slots[slot] = (channel, value)
            updates = []
            for slot, (channel, value) in slots.items():
                digest = self._write_hash(channel, value)
                existing = (
                    await session.execute(
                        select(CheckpointWriteReceiptRow).where(
                            CheckpointWriteReceiptRow.thread_id == self._thread_id,
                            CheckpointWriteReceiptRow.checkpoint_ns == self._checkpoint_ns,
                            CheckpointWriteReceiptRow.checkpoint_id == checkpoint_id,
                            CheckpointWriteReceiptRow.task_id == task_id,
                            CheckpointWriteReceiptRow.write_key == _slot_key(slot),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.content_hash != digest and not upsert:
                        raise CheckpointFenceError("同一 pending write 键出现不同内容")
                    updates.append((existing, digest))
                else:
                    row = CheckpointWriteReceiptRow(
                        receipt_id=uuid4(),
                        thread_id=self._thread_id,
                        checkpoint_ns=self._checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        task_id=task_id,
                        write_key=_slot_key(slot),
                        content_hash=digest,
                        writer_epoch=self._writer_epoch,
                        owner_generation=self._owner_generation,
                        control_version=self._control_version,
                    )
                    updates.append((row, digest))
            # Validate conflicts before mutating the independently committed official Saver.
            await self._inner.aput_writes(write_config, writes, task_id, task_path)
            await self._verify_stored_slots(session, checkpoint_id, task_id, slots)
            for row, digest in updates:
                row.content_hash = digest
                row.writer_epoch = self._writer_epoch
                row.owner_generation = self._owner_generation
                row.control_version = self._control_version
                session.add(row)

    def _write_hash(self, channel: str, value: Any) -> str:
        # Bind the channel and preserve framework types such as Send and bytes.
        kind, payload = self.serde.dumps_typed((channel, value))
        return hashlib.sha256(kind.encode("utf-8") + b"\0" + payload).hexdigest()

    def _writes_table(self) -> str:
        schema = self._checkpoint_schema.replace('"', '""')
        return f'"{schema}".checkpoint_writes'

    async def _verify_stored_slots(
        self, session: Any, checkpoint_id: str, task_id: str,
        slots: dict[int, tuple[str, Any]],
    ) -> None:
        if not slots or not isinstance(self._inner, AsyncPostgresSaver):
            return
        # DO NOTHING may retain orphan writes from a failed receipt transaction.
        # Never issue a receipt for requested bytes that the Saver did not persist.
        query = text(
            f"SELECT idx,channel,type,blob FROM {self._writes_table()} "
            "WHERE thread_id=:thread AND checkpoint_ns=:ns AND checkpoint_id=:checkpoint "
            "AND task_id=:task AND idx IN :slots"
        ).bindparams(bindparam("slots", expanding=True))
        rows = (await session.execute(query, {
            "thread": self._thread_id, "ns": self._checkpoint_ns,
            "checkpoint": checkpoint_id, "task": task_id, "slots": list(slots),
        })).mappings().all()
        stored = {row["idx"]: row for row in rows}
        for slot, (channel, value) in slots.items():
            row = stored.get(slot)
            if row is None or row["channel"] != channel:
                raise CheckpointFenceError("pending write 位置与实际保存内容不一致，拒绝签发收据")
            actual = self.serde.loads_typed((row["type"], bytes(row["blob"])))
            if self._write_hash(channel, actual) != self._write_hash(channel, value):
                raise CheckpointFenceError("pending write 实际内容冲突，拒绝为未落库内容签发收据")

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = dict(config.get("configurable") or {})
        if not configurable.get("checkpoint_id"):
            async with self._db.session() as session:
                session_row = (
                    await session.execute(
                        select(SessionRow).where(SessionRow.thread_id == self._thread_id)
                    )
                ).scalar_one_or_none()
                head = (
                    await session.execute(
                        select(CheckpointHeadRow).where(
                            CheckpointHeadRow.thread_id == self._thread_id,
                            CheckpointHeadRow.checkpoint_ns == self._checkpoint_ns,
                        )
                    )
                ).scalar_one_or_none()
                if head is not None and head.accepted_checkpoint_id:
                    configurable["checkpoint_id"] = head.accepted_checkpoint_id
                elif session_row is None:
                    raise StaleWriterError("thread 不存在，拒绝恢复")
            config = {**config, "configurable": configurable}
        result = await self._inner.aget_tuple(config)
        if result is None:
            return None
        return await self._filter_pending_writes(result)

    async def _filter_pending_writes(self, result: CheckpointTuple) -> CheckpointTuple:
        """只保留有已提交收据、内容哈希与所有权匹配的 pending writes。"""
        pending = list(result.pending_writes or [])
        if not pending:
            return result
        checkpoint_id = str(
            (result.config.get("configurable") or {}).get("checkpoint_id")
            or (result.checkpoint or {}).get("id")
            or ""
        )
        if not checkpoint_id:
            return result._replace(pending_writes=[])
        async with self._db.session() as session:
            receipts = list(
                (
                    await session.execute(
                        select(CheckpointWriteReceiptRow).where(
                            CheckpointWriteReceiptRow.thread_id == self._thread_id,
                            CheckpointWriteReceiptRow.checkpoint_ns == self._checkpoint_ns,
                            CheckpointWriteReceiptRow.checkpoint_id == checkpoint_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            positions = None
            if isinstance(self._inner, AsyncPostgresSaver):
                positions = (await session.execute(text(
                    f"SELECT task_id,idx,channel FROM {self._writes_table()} "
                    "WHERE thread_id=:thread AND checkpoint_ns=:ns AND checkpoint_id=:checkpoint "
                    "ORDER BY task_id,idx"
                ), {"thread": self._thread_id, "ns": self._checkpoint_ns,
                    "checkpoint": checkpoint_id})).mappings().all()
                if len(positions) != len(pending):
                    # A concurrent change made the two read views incomparable.
                    return result._replace(pending_writes=[])
        by_key = {(row.task_id, row.write_key): row for row in receipts}
        channel_counts = Counter(
            (str(item[0]), str(item[1])) if isinstance(item, tuple)
            else (str(getattr(item, "task_id", "")), str(getattr(item, "channel", "")))
            for item in pending
        )
        # Non-Postgres test doubles do not expose physical write indices.
        fallback = Counter((row.task_id, row.content_hash) for row in receipts
            if row.write_key.startswith("v2:slot:") and self._receipt_is_current(row))
        kept = []
        for index, item in enumerate(pending):
            if isinstance(item, tuple):
                task_id = str(item[0] if len(item) > 0 else "")
                channel = str(item[1] if len(item) > 1 else "")
                value = item[2] if len(item) > 2 else None
            else:
                task_id = str(getattr(item, "task_id", "") or "")
                channel = str(getattr(item, "channel", "") or "")
                value = getattr(item, "value", None)
            if positions is not None:
                position = positions[index]
                if str(position["task_id"]) != task_id or position["channel"] != channel:
                    return result._replace(pending_writes=[])
                receipt = by_key.get((task_id, _slot_key(position["idx"])))
                if receipt is not None:
                    if (self._receipt_is_current(receipt)
                            and receipt.content_hash == self._write_hash(channel, value)):
                        kept.append(item)
                    continue
            else:
                key = (task_id, self._write_hash(channel, value))
                if fallback[key] > 0:
                    fallback[key] -= 1
                    kept.append(item)
                    continue
            # Legacy receipts lack slot identity: accept only an unambiguous channel.
            receipt = by_key.get((task_id, channel))
            if receipt is None:
                continue
            if channel_counts[(task_id, channel)] != 1:
                continue
            if receipt.content_hash != _content_hash(value):
                continue
            if not self._receipt_is_current(receipt):
                continue
            kept.append(item)
        if len(kept) == len(pending):
            return result
        return result._replace(pending_writes=kept)

    def _receipt_is_current(self, receipt: CheckpointWriteReceiptRow) -> bool:
        return (int(receipt.writer_epoch) == self._writer_epoch
                and int(receipt.owner_generation) == self._owner_generation
                and int(receipt.control_version) == self._control_version)

    def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        return self._inner.alist(config, filter=filter, before=before, limit=limit)

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._inner.get_next_version(current, channel)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._inner.adelete_thread(thread_id)


__all__ = [
    "CheckpointFenceError",
    "FencedCheckpointSaver",
    "StaleWriterError",
]
