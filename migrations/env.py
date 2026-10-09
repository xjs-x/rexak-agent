"""Alembic 环境。

约束（§5.1）：
- Alembic **只**管理业务 `app` schema；
- **不要**让 Alembic 修改框架内部表（`agent_checkpoint` / `agent_memory`）；
- 迁移从空库可成功，对已有数据升级不清库。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from agent_workbench.config.settings import get_settings
from agent_workbench.persistence.tables import APP_METADATA, APP_SCHEMA

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = APP_METADATA
settings = get_settings()


def _sync_url() -> str:
    """Alembic 使用同步驱动。"""
    return settings.database_url.replace("postgresql+psycopg://", "postgresql+psycopg://", 1)


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ARG001
    """只处理 app schema 下的对象，避免误碰框架表。"""
    schema = getattr(obj, "schema", None)
    # 只处理 app schema 下的对象；框架表由框架自己管理
    return not (type_ == "table" and schema and schema != APP_SCHEMA)


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=APP_SCHEMA,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        # 业务 schema 必须存在；由 `workbench db-init` 先创建
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{APP_SCHEMA}"')
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=APP_SCHEMA,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
