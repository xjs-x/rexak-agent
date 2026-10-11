"""建连重试只处理握手超时；认证错误及任务取消不重试。"""

from types import SimpleNamespace

import psycopg
import pytest

from agent_workbench.persistence.connection import connect_postgres


async def test_transient_handshake_timeout_retries_without_executing_queries(monkeypatch):
    calls = []
    connection = SimpleNamespace()

    async def connect(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise psycopg.errors.ConnectionTimeout("暂时握手超时")
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    assert await connect_postgres("dbname=example", connect_timeout=1) is connection
    assert len(calls) == 2
    assert all(call["connect_timeout"] == 1 for call in calls)


async def test_invalid_credentials_are_not_retried(monkeypatch):
    calls = []

    async def connect(*args, **kwargs):
        calls.append(1)
        raise psycopg.errors.InvalidPassword("凭据错误")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(psycopg.errors.InvalidPassword):
        await connect_postgres("dbname=example")
    assert len(calls) == 1
