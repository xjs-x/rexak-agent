"""框架与业务 schema 的初始化。

权威归属（§5.1）：
- `agent_checkpoint`：框架 Checkpointer 隔离 schema；
- `agent_memory`：框架 Store 隔离 schema；
- `app`：业务表，由 Alembic 管理。

**框架 `setup()` / 迁移由单独初始化命令执行，不能每个 API 请求调用。**
**不要让 Alembic 修改框架内部表。**

Checkpointer 与 Store 使用独立的 psycopg 连接（`autocommit=True`，`row_factory=dict_row`），
并通过连接参数 `options=-c search_path=<schema>` 完成 schema 隔离。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agent_workbench.config.settings import REPO_ROOT, Settings, get_settings
from agent_workbench.persistence.connection import connect_postgres


class BusinessSchemaMismatchError(RuntimeError):
    """业务库版本与当前代码不匹配，必须先执行显式迁移。"""


def expected_business_revisions(repo_root: Path = REPO_ROOT) -> set[str]:
    """从实际迁移图读取当前代码的 head，不另维护硬编码版本号。"""
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def assert_business_revisions(installed: set[str], expected: set[str]) -> None:
    if not expected or installed != expected:
        raise BusinessSchemaMismatchError(
            f"业务库 schema 版本不匹配：数据库 {sorted(installed)}，"
            f"代码 {sorted(expected)}。请先备份，再运行 workbench db-init；"
            "未完成升级前拒绝启动 API/Runner。"
        )


async def assert_business_schema_current(
    session: AsyncSession, settings: Settings | None = None
) -> set[str]:
    """只读核对 Alembic 版本；不在服务启动时自动修改表。"""
    cfg = settings or get_settings()
    table = (
        await session.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = 'alembic_version'"
            ),
            {"schema": cfg.app_schema},
        )
    ).scalar_one_or_none()
    installed: set[str] = set()
    if table is not None:
        quoted_schema = '"' + cfg.app_schema.replace('"', '""') + '"'
        installed = set(
            (await session.execute(text(f"SELECT version_num FROM {quoted_schema}.alembic_version")))
            .scalars()
            .all()
        )
    expected = expected_business_revisions()
    assert_business_revisions(installed, expected)
    return installed


def _psycopg_conninfo(database_url: str) -> str:
    """把 SQLAlchemy 异步 URL 转成 psycopg 可用的 conninfo。"""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def create_schemas(settings: Settings) -> list[str]:
    """创建三个 schema。幂等。"""
    engine = create_async_engine(
        settings.database_url,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )
    created: list[str] = []
    try:
        async with engine.connect() as conn:
            for schema in (
                settings.app_schema,
                settings.checkpoint_schema,
                settings.memory_schema,
            ):
                exists = (
                    await conn.execute(
                        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :name"),
                        {"name": schema},
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                # schema 名来自配置而非用户输入；仍使用标识符引用防止注入
                quoted = '"' + schema.replace('"', '""') + '"'
                await conn.execute(text(f"CREATE SCHEMA {quoted}"))
                created.append(schema)
    finally:
        await engine.dispose()
    return created


@contextlib.asynccontextmanager
async def open_checkpointer_connection(
    settings: Settings,
) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    conn = await connect_postgres(
        _psycopg_conninfo(settings.database_url),
        autocommit=True,
        connect_timeout=settings.db_connect_timeout_seconds,
        # psycopg 的 dict_row 类型标注为 TupleRow 族，与运行时行为不符；这是 stub 限制。
        row_factory=dict_row,  # type: ignore[arg-type]
        options=f"-c search_path={settings.checkpoint_schema}",
    )
    try:
        yield conn
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def open_store_connection(settings: Settings) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    conn = await connect_postgres(
        _psycopg_conninfo(settings.database_url),
        autocommit=True,
        connect_timeout=settings.db_connect_timeout_seconds,
        # psycopg 的 dict_row 类型标注为 TupleRow 族，与运行时行为不符；这是 stub 限制。
        row_factory=dict_row,  # type: ignore[arg-type]
        options=f"-c search_path={settings.memory_schema}",
    )
    try:
        yield conn
    finally:
        await conn.close()


async def setup_framework_tables(settings: Settings | None = None) -> dict[str, Any]:
    """执行框架 `setup()`。**只在 `workbench db-init` 中调用。**

    重复执行幂等。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    cfg = settings or get_settings()
    report: dict[str, Any] = {
        "checkpoint_schema": cfg.checkpoint_schema,
        "memory_schema": cfg.memory_schema,
    }

    async with open_checkpointer_connection(cfg) as conn:
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
        report["checkpointer"] = "ok"

    async with open_store_connection(cfg) as conn:
        store = AsyncPostgresStore(conn)
        await store.setup()
        report["store"] = "ok"

    return report


async def business_schema_status(
    session: AsyncSession, settings: Settings | None = None
) -> dict[str, Any]:
    """检查业务表是否已创建（Alembic 是否已升级）。"""
    cfg = settings or get_settings()
    rows = (
        (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema ORDER BY table_name"
                ),
                {"schema": cfg.app_schema},
            )
        )
        .scalars()
        .all()
    )
    return {"schema": cfg.app_schema, "tables": list(rows), "table_count": len(rows)}
