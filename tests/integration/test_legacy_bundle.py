"""原始 B0 schema 可只读导出；不能先迁移试验库或补造新机制资料。"""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.run_bundle import (
    collect_run_bundle,
    verify_run_bundle,
    write_run_bundle,
)
from agent_workbench.persistence.bootstrap import create_schemas, open_checkpointer_connection
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository

pytestmark = pytest.mark.integration


async def test_baseline_database_exports_without_upgrade(settings, tmp_path):
    name = "workbench_test_legacy_" + uuid4().hex
    base_url = make_url(settings.database_url)
    admin_url = base_url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    legacy_url = base_url.set(database=name).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    legacy = Database(
        settings.model_copy(update={"database_url": legacy_url, "data_root": tmp_path / "data"})
    )
    try:
        with psycopg.connect(
            legacy_url.replace("postgresql+psycopg", "postgresql"), autocommit=True
        ) as connection:
            connection.execute("CREATE SCHEMA app")
        environment = {**os.environ, "WORKBENCH_DATABASE_URL": legacy_url, "PYTHONUTF8": "1"}
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "f42cb7a9e180"],
            env=environment,
            capture_output=True,
            timeout=40,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8")
        await create_schemas(legacy.settings)
        async with open_checkpointer_connection(legacy.settings) as connection:
            await AsyncPostgresSaver(connection).setup()
        await legacy.start()
        async with legacy.transaction() as db:
            project = await ProjectRepository(db).create(
                owner_id="legacy", name="baseline", target_description="local", default_scope={}
            )
            task = await SessionRepository(db).create(
                project_id=project.id, title="旧会话", scope_snapshot={}, policy_snapshot={}
            )
            run = await create_run(
                db,
                session_id=task.id,
                objective="旧阶段",
                provider_id="fake",
                model_name="scripted",
                config_snapshot={},
            )
            await db.execute(
                text("""INSERT INTO app.leads
                (id,session_id,hypothesis,status,priority,identity_refs,evidence_refs,priority_rationale,revision)
                VALUES (:id,:session,'原始线索','new',0,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,1)"""),
                {"id": uuid4(), "session": task.id},
            )
        payload = await collect_run_bundle(legacy, run.id)
        assert payload["session_knowledge"]["leads"][0]["merged_into_id"] is None
        assert len(payload["snapshot"]["missing_schema_fields"]) == 4
        output = write_run_bundle(tmp_path / "bundle", payload, ArtifactStore(legacy.settings))
        assert verify_run_bundle(output) == []
        async with legacy.session() as db:
            assert (
                await db.execute(text("SELECT version_num FROM app.alembic_version"))
            ).scalar_one() == "f42cb7a9e180"
    finally:
        await legacy.close()
        # 只清理本测试明确创建的随机数据库，不接受用户数据库名。
        assert (
            name.startswith("workbench_test_legacy_")
            and len(name) == len("workbench_test_legacy_") + 32
        )
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
