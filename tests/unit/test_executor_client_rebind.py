"""A Kali recreation must not leave the Runner on its old SSH listener."""

from __future__ import annotations

import httpx
import pytest

from agent_workbench.execution.client import ExecutorConnectionError, HttpExecutorClient
from agent_workbench.execution.tunnel import SshTunnel


class _Tunnel:
    pass


async def test_post_retries_once_after_definite_local_connect_failure(monkeypatch):
    attempts = 0

    async def connect_failed(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("old local forwarding listener closed")

    async def success(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"ok": True})

    client = HttpExecutorClient(_Tunnel())  # type: ignore[arg-type]
    client._control = httpx.AsyncClient(base_url="http://127.0.0.1:10001", transport=httpx.MockTransport(connect_failed))
    rebinds = 0

    async def rebind(*, failed_client: httpx.AsyncClient | None = None) -> None:
        nonlocal rebinds
        rebinds += 1
        assert failed_client is client._control
        client._control = httpx.AsyncClient(base_url="http://127.0.0.1:10002", transport=httpx.MockTransport(success))

    monkeypatch.setattr(client, "_rebind", rebind)
    try:
        response = await client._request("POST", "/execution/v1/operations", json_body={"operation_id": "stable"})
        assert response.json() == {"ok": True}
        assert (attempts, rebinds) == (2, 1)
    finally:
        await client.close()


async def test_post_read_timeout_is_not_replayed(monkeypatch):
    async def read_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost")

    client = HttpExecutorClient(_Tunnel())  # type: ignore[arg-type]
    client._control = httpx.AsyncClient(base_url="http://127.0.0.1:10001", transport=httpx.MockTransport(read_timeout))

    async def must_not_rebind(**_kwargs):
        raise AssertionError("response loss must not retry a mutation")

    monkeypatch.setattr(client, "_rebind", must_not_rebind)
    try:
        with pytest.raises(ExecutorConnectionError, match="响应不确定"):
            await client._request("POST", "/execution/v1/operations", json_body={"operation_id": "stable"})
    finally:
        await client.close()


async def test_tunnel_reconnect_drops_old_listener(monkeypatch):
    tunnel = SshTunnel()
    tunnel._state.connected = True
    tunnel._state.local_port = 10001

    async def connect() -> None:
        tunnel._state.connected = True
        tunnel._state.local_port = 10002

    monkeypatch.setattr(tunnel, "_connect", connect)
    assert await tunnel.reconnect_with_backoff(max_attempts=1) == "http://127.0.0.1:10002"
