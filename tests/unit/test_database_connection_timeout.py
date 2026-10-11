"""TCP 可连接但不回应 PostgreSQL 握手时，各持久化连接必须有边界。"""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from agent_workbench.persistence.bootstrap import (
    open_checkpointer_connection,
    open_store_connection,
)
from agent_workbench.persistence.db import Database


@pytest.mark.parametrize("path", ["business", "checkpoint", "store"])
async def test_unresponsive_database_handshake_times_out(settings, path):
    writers = []

    def accept(_reader, writer):
        writers.append(writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = settings.model_copy(
        update={
            "database_url": make_url(settings.database_url)
            .set(port=port)
            .render_as_string(hide_password=False),
            "db_connect_timeout_seconds": 1,
        }
    )
    database = Database(config)
    try:
        # 外层边界只保护测试；如果连接实现无边界，会抛错而非伪装成通过。
        async with asyncio.timeout(7):
            if path == "business":
                await database.start()
                with pytest.raises(OperationalError):
                    await database.ping()
            else:
                opener = (
                    open_checkpointer_connection if path == "checkpoint" else open_store_connection
                )
                with pytest.raises(psycopg.OperationalError):
                    async with opener(config):
                        pytest.fail("无回应端口不应完成数据库握手")
    finally:
        await database.close()
        server.close()
        for writer in writers:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        await asyncio.wait_for(server.wait_closed(), timeout=2)
