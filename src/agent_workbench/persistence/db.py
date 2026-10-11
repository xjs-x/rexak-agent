"""数据库连接与事务边界。

规则（BUILD_PLAN §4.3）：
- 数据库事务保持短小；**不在持有行锁或事务时等待模型、SSH、文件上传或浏览器操作**；
- 连接池数量有明确上限，启动与关闭生命周期成对。

本模块不 import `agent`，避免循环依赖。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.persistence.connection import connect_postgres


class Database:
    """异步数据库句柄。

    只暴露 3 个能力：读 session、写事务、引擎。业务逻辑不直接拼 SQL 之外的东西。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database 未启动：请先 await database.start()")
        return self._engine

    async def start(self) -> None:
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            self._settings.database_url,
            pool_size=self._settings.db_pool_min,
            max_overflow=max(0, self._settings.db_pool_max - self._settings.db_pool_min),
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={
                "connect_timeout": self._settings.db_connect_timeout_seconds,
                "async_creator_fn": connect_postgres,
            },
            future=True,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("Database 未启动：请先 await database.start()")
        return self._sessionmaker

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """只读或自行控制提交的 session。"""
        async with self.session_factory()() as session:
            yield session

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """短事务边界。异常时回滚。

        **禁止**在该上下文内执行模型/SSH/浏览器调用。
        """
        async with self.session_factory()() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> dict[str, Any]:
        """健康检查。返回数据库版本与当前 schema。"""
        from sqlalchemy import text

        async with self.session() as session:
            version = (await session.execute(text("select version()"))).scalar_one()
            current_schema = (await session.execute(text("select current_schema()"))).scalar_one()
        return {"server_version": str(version).split(",")[0], "current_schema": current_schema}


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database()
    return _database


def reset_database_for_tests() -> None:
    """测试钩子：丢弃进程内单例，让下一个用例自行启动/关闭。"""
    global _database
    _database = None
