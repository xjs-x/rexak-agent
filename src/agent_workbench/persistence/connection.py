"""只在 PostgreSQL 建连握手超时时重试；从不重放 SQL 或事务。"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg


async def connect_postgres(
    *args: Any, attempts: int = 2, **kwargs: Any
) -> psycopg.AsyncConnection[Any]:
    for attempt in range(attempts):
        try:
            return await psycopg.AsyncConnection.connect(*args, **kwargs)
        except psycopg.errors.ConnectionTimeout:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.2)
    raise ValueError("建连尝试次数必须为正数")
