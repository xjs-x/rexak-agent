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
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from sqlalchemy import select

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
    ) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner
        self._db = database
        self._run_id = run_id
        self._thread_id = thread_id
        self._checkpoint_ns = checkpoint_ns
        self._owner_generation = int(owner_generation)
        self._control_version = int(control_version)
        self._writer_epoch = int(writer_epoch)

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
                accepted_checkpoint_id="",
                writer_epoch=self._writer_epoch,
                owner_generation=self._owner_generation,
                control_version=self._control_version,
                accepted_at=datetime.now(UTC),
            )
            session.add(head)
            await session.flush()
        elif int(head.writer_epoch) != self._writer_epoch:
            raise StaleWriterError("writer epoch 已过期，拒绝旧 writer 写入")
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
            await self._inner.aput_writes(write_config, writes, task_id, task_path)
            for write_key, value in writes:
                digest = _content_hash(value)
                existing = (
                    await session.execute(
                        select(CheckpointWriteReceiptRow).where(
                            CheckpointWriteReceiptRow.thread_id == self._thread_id,
                            CheckpointWriteReceiptRow.checkpoint_ns == self._checkpoint_ns,
                            CheckpointWriteReceiptRow.checkpoint_id == checkpoint_id,
                            CheckpointWriteReceiptRow.task_id == task_id,
                            CheckpointWriteReceiptRow.write_key == str(write_key),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.content_hash != digest:
                        raise CheckpointFenceError("同一 pending write 键出现不同内容")
                    continue
                session.add(
                    CheckpointWriteReceiptRow(
                        receipt_id=uuid4(),
                        thread_id=self._thread_id,
                        checkpoint_ns=self._checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        task_id=task_id,
                        write_key=str(write_key),
                        content_hash=digest,
                        writer_epoch=self._writer_epoch,
                        owner_generation=self._owner_generation,
                        control_version=self._control_version,
                    )
                )

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
        return await self._inner.aget_tuple(config)

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
