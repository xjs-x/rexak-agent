"""回环 SSH 隧道不能被宿主机代理配置劫持。"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from agent_workbench.execution.client import HttpExecutorClient


class _Tunnel:
    async def ensure(self) -> str:
        return "http://127.0.0.1:18791"

    async def reconnect_with_backoff(self) -> str:
        return "http://127.0.0.1:18792"


@pytest.mark.asyncio
async def test_executor_client_ignores_environment_proxy_on_start_and_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:19999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:19999")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:19999")
    monkeypatch.setenv("NO_PROXY", "")
    real_client = httpx.AsyncClient
    with patch("agent_workbench.execution.client.httpx.AsyncClient", wraps=real_client) as create:
        client = HttpExecutorClient(_Tunnel())  # type: ignore[arg-type]
        try:
            await client.start()
            await client._rebind()
        finally:
            await client.close()

    assert len(create.call_args_list) == 4
    assert all(call.kwargs["trust_env"] is False for call in create.call_args_list)
