"""共享测试夹具。

原则：
- 需要**真实 PostgreSQL** 的测试标 `@pytest.mark.integration`，无数据库时
  应明确 skip 而不是假装通过（ACCEPTANCE §1："NOT_RUN 不能算通过"）；
- 真实模型测试标 `@pytest.mark.real_model`，无凭据时 skip；
- 真实执行岛测试标 `@pytest.mark.real_executor`。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from agent_workbench.platform_compat import configure_event_loop_policy

# 入口点必须设置事件循环策略（Windows 上 psycopg 异步需要 Selector）
configure_event_loop_policy()


@pytest.fixture(scope="session")
def settings():
    from agent_workbench.config.settings import get_settings

    return get_settings()


@pytest.fixture
async def database(settings) -> AsyncIterator[object]:
    """真实数据库句柄。不可用时直接失败，不静默降级。"""
    from agent_workbench.persistence.db import Database

    db = Database(settings)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def unique_name() -> str:
    return f"test-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def xiaomi_available() -> bool:
    from agent_workbench.config.secrets import build_secret_store

    return build_secret_store().exists("env:XIAOMI_API_KEY")


def pytest_collection_modifyitems(config, items):
    """没有真实凭据时，把 real_model 用例标记为 skip（报告里仍会显示为 skipped）。"""
    has_key = bool(os.environ.get("XIAOMI_API_KEY"))
    for item in items:
        if "real_model" in item.keywords and not has_key:
            item.add_marker(pytest.mark.skip(reason="未配置 XIAOMI_API_KEY：标为 NOT_RUN"))
