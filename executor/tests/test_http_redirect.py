"""重定向逐跳过范围门，跨 origin 时不转发显式认证头。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from workbench_executor.http_sessions import HttpSessionRegistry
from workbench_executor.policy import NetworkPolicy, ScopeRuleSpec
from workbench_executor.settings import ExecutorSettings

from conftest import target_is_up


@pytest.mark.skipif(not target_is_up(), reason="本地 fixture 目标未运行")
def test_cross_origin_redirect_drops_authorization() -> None:
    asyncio.run(_exercise_cross_origin_redirect())


async def _exercise_cross_origin_redirect() -> None:
    settings = ExecutorSettings(token="test-token")
    policy = NetworkPolicy(
        [
            ScopeRuleSpec(scheme="http", host="127.0.0.1", port=8899),
            ScopeRuleSpec(scheme="http", host="localhost", port=8899),
        ],
        [],
        settings,
    )
    sessions = HttpSessionRegistry(policy)
    try:
        response = await sessions.request(
            identity_id="alice",
            method="GET",
            url="http://127.0.0.1:8899/redirect-localhost",
            headers={"Authorization": "Bearer top-secret", "Host": "127.0.0.1:8899"},
        )
    finally:
        await sessions.close()
    assert response.status_code == 200
    body: dict[str, Any] = json.loads(response.body)
    assert body["authorization"] is None
    assert body["host"] == "localhost:8899"


@pytest.mark.skipif(not target_is_up(), reason="本地 fixture 目标未运行")
def test_large_response_is_bounded_while_streaming() -> None:
    async def exercise() -> None:
        settings = ExecutorSettings(token="test-token")
        policy = NetworkPolicy(
            [ScopeRuleSpec(scheme="http", host="127.0.0.1", port=8899)], [], settings
        )
        sessions = HttpSessionRegistry(policy)
        try:
            response = await sessions.request(
                identity_id="anonymous",
                method="GET",
                url="http://127.0.0.1:8899/large",
                max_body_bytes=1024,
            )
        finally:
            await sessions.close()
        assert response.status_code == 200
        assert len(response.body) == 1024
        assert response.truncated is True

    asyncio.run(exercise())
