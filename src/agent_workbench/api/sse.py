"""SSE 事件流。

一致性要求（§12.2）：
- 状态变更与对应持久事件**同一业务事务**提交，提交后才广播；
- 每个 session 的事件顺序可安全续读：`session_seq` 在短事务内分配并提交；
- GET snapshot 返回 `event_cursor`，随后订阅**大于**该 cursor 的事件，不漏更新；
- 客户端以 `event_id`/`seq` 去重，按服务端 `session_seq` 排序，**不依赖跨机器时钟**；
- 历史已清理导致游标过期时返回明确 **resync** 要求；
- 心跳、防代理缓冲、重连与**慢客户端背压**；UI 断线**不阻塞 Runner**。

实现要点：生成器**只读数据库**（轮询），Runner 只写数据库。
因此慢客户端只会自己落后，永远不会阻塞运行中的任务。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_workbench.contracts.events import EVENT_SCHEMA_VERSION, EventType, heartbeat_frame
from agent_workbench.observability.events import (
    latest_seq,
    min_available_seq,
    read_events_after,
)

#: 轮询间隔。与本机单用户场景匹配；不引入额外消息中间件。
POLL_INTERVAL_SECONDS = 0.4
#: 单次补读上限，避免一次性拉爆内存
REPLAY_BATCH = 300
#: 心跳间隔：防代理缓冲 + 让客户端知道连接还活着
HEARTBEAT_SECONDS = 15.0


def resync_frame(session_id: UUID, *, reason: str, latest: int, earliest: int | None) -> str:
    """游标过期时要求客户端重新加载快照。"""

    from agent_workbench.contracts.events import EventEnvelope

    envelope = EventEnvelope(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=UUID(int=0),
        session_seq=latest,
        session_id=session_id,
        run_id=None,
        type=EventType.STREAM_RESYNC_REQUIRED,
        occurred_at=_now(),
        payload={
            "reason": reason,
            "earliest_available_seq": earliest,
            "latest_seq": latest,
            "action": "请重新 GET /api/sessions/{id}/snapshot 并使用返回的 event_cursor 重新订阅",
        },
    )
    return f"event: {envelope.type}\ndata: {envelope.model_dump_json()}\n\n"


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def event_stream(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: UUID,
    after_seq: int,
) -> AsyncIterator[str]:
    """按 `session_seq > after_seq` 顺序推送。

    客户端断开时生成器被取消，**不会**影响 Runner。
    """
    cursor = max(0, after_seq)
    since_heartbeat = 0.0

    # 先做一次过期检查
    async with session_factory() as session:
        earliest = await min_available_seq(session, session_id)
        latest = await latest_seq(session, session_id)
    if earliest is not None and cursor < earliest - 1:
        yield resync_frame(session_id, reason="cursor_expired", latest=latest, earliest=earliest)
        cursor = latest

    while True:
        produced = False
        async with session_factory() as session:
            events = await read_events_after(
                session, session_id=session_id, after_seq=cursor, limit=REPLAY_BATCH
            )
        for envelope in events:
            cursor = max(cursor, envelope.session_seq)
            yield envelope.to_sse()
            produced = True

        if produced:
            since_heartbeat = 0.0
            # 还有积压就继续读，不等待（背压由客户端消费速度决定）
            if len(events) >= REPLAY_BATCH:
                continue
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        since_heartbeat += POLL_INTERVAL_SECONDS
        if since_heartbeat >= HEARTBEAT_SECONDS:
            since_heartbeat = 0.0
            yield heartbeat_frame()


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    # 关闭 nginx 缓冲，避免事件被积压
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
