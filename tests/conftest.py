"""共享测试夹具。

原则：
- 需要**真实 PostgreSQL** 的测试标 `@pytest.mark.integration`，无数据库时
  应明确 skip 而不是假装通过（ACCEPTANCE §1："NOT_RUN 不能算通过"）；
- 真实模型测试标 `@pytest.mark.real_model`，无凭据时 skip；
- 真实执行岛测试标 `@pytest.mark.real_executor`。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workbench.platform_compat import configure_event_loop_policy

# 入口点必须设置事件循环策略（Windows 上 psycopg 异步需要 Selector）
configure_event_loop_policy()


@pytest.fixture(scope="session", autouse=True)
def _isolated_test_environment(request):
    """数据库测试使用本次会话独占的空库和工件目录。

    恢复测试会扫描全部 run，不能让它们访问日常工作库。只运行单测时无需数据库。
    清理仅针对本函数成功创建的随机数据库，不接受外部指定的删除目标。
    """
    needs_database = any(
        item.get_closest_marker("integration") is not None or "database" in item.fixturenames
        for item in request.session.items
    )
    if not needs_database:
        yield
        return

    from agent_workbench.config.settings import get_settings
    from agent_workbench.persistence.db import reset_database_for_tests

    base = get_settings()
    base_url = make_url(base.database_url)
    database_name = f"workbench_test_{uuid.uuid4().hex}"
    admin_url = base_url.set(drivername="postgresql", database="postgres")
    test_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    previous = {
        key: os.environ.get(key) for key in ("WORKBENCH_DATABASE_URL", "WORKBENCH_DATA_ROOT")
    }
    created = False
    try:
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False),
            autocommit=True,
            connect_timeout=5,
        ) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            created = True
        with tempfile.TemporaryDirectory(prefix="workbench-test-data-") as data_root:
            os.environ["WORKBENCH_DATABASE_URL"] = test_url
            os.environ["WORKBENCH_DATA_ROOT"] = data_root
            get_settings.cache_clear()
            reset_database_for_tests()
            initialized = subprocess.run(
                [sys.executable, "-m", "agent_workbench.cli", "db-init"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                check=False,
                timeout=90,
            )
            if initialized.returncode:
                detail = initialized.stderr.decode("utf-8", errors="backslashreplace")[-2000:]
                raise RuntimeError(f"隔离测试库初始化失败：{detail}")
            yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        reset_database_for_tests()
        if created:
            for attempt in range(3):
                try:
                    with psycopg.connect(
                        admin_url.render_as_string(hide_password=False),
                        autocommit=True,
                        connect_timeout=5,
                    ) as connection:
                        connection.execute(
                            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                                sql.Identifier(database_name)
                            )
                        )
                    break
                except psycopg.OperationalError as exc:
                    if attempt == 2:
                        raise RuntimeError(
                            f"隔离测试库清理失败，残留库 {database_name}：{type(exc).__name__}"
                        ) from None
                    time.sleep(0.2)


@pytest.fixture(scope="session")
def settings(_isolated_test_environment):
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
