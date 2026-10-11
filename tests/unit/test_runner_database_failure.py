"""业务库瞬断是恢复事件，不能被误报成测试失败结论。"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from openai import APIConnectionError
from sqlalchemy.exc import OperationalError

from agent_workbench.contracts.enums import RunStatus
from agent_workbench.runtime.runner import AgentRunner


@pytest.mark.asyncio
async def test_operational_error_enters_recovery_without_failure_event(settings) -> None:
    runner = AgentRunner(settings=settings)
    error = OperationalError("SELECT 1", None, Exception("connection closed"))
    status, detail = await runner._classify_exception(uuid4(), error)
    assert status is RunStatus.RECOVERING
    assert "数据库连接中断" in detail


@pytest.mark.asyncio
async def test_model_connection_error_enters_recovery_without_failure_event(settings) -> None:
    runner = AgentRunner(settings=settings)
    # SDK 版本可能使用 httpx 或 httpx2；此处只验证异常分类，不发起请求。
    error = APIConnectionError(
        request=cast(Any, httpx.Request("POST", "https://example.test/v1/chat"))
    )
    status, detail = await runner._classify_exception(uuid4(), error)
    assert status is RunStatus.RECOVERING
    assert "模型服务连接中断" in detail
