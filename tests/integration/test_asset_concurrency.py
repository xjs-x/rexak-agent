"""并行领域登记的原子性：真实 PostgreSQL、两个独立连接与同步屏障。

必须证明：
- 两个工具同时登记同一入口，两个业务操作**都成功**，资产只有一行；
- 一个调用失败不能撤销另一个已经提交的观察；
- 不同方法、不同身份不合并；
- 重放返回原结果、显式重测产生新的逻辑操作身份。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.tools import ToolRuntime
from sqlalchemy import func, select

from agent_workbench.domain.assets import record_asset
from agent_workbench.domain.observations import record_observation_ref
from agent_workbench.persistence.tables import AssetRow, LeadRow, ObservationRow, ToolOperationRow
from agent_workbench.tools.task import record_lead
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def test_parallel_registration_of_same_entry_commits_once(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    url = "http://example.test/api/items"

    async def register(tag: str) -> tuple[Any, bool]:
        async with database.transaction() as db:
            row, created = await record_asset(
                db,
                session_id=session_id,
                url=url,
                method="POST",
                identity_ids=["identity-a"],
                notes=tag,
            )
            return row.id, created

    results = await asyncio.gather(register("first"), register("second"))
    assert len({item[0] for item in results}) == 1, "同一入口必须收敛到同一行"
    assert sum(1 for item in results if item[1]) == 1, "恰好一次是插入"
    async with database.session() as db:
        rows = (
            (
                await db.execute(
                    select(AssetRow).where(
                        AssetRow.session_id == session_id, AssetRow.path == "/api/items"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].method == "POST" and rows[0].identity_key == "identity-a"


async def test_different_method_and_identity_are_not_merged(database, unique_name):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    url = "http://example.test/api/items"
    async with database.transaction() as db:
        get_row, _ = await record_asset(db, session_id=session_id, url=url, method="GET")
        other_method, _ = await record_asset(db, session_id=session_id, url=url, method="PUT")
        other_identity, _ = await record_asset(
            db, session_id=session_id, url=url, method="GET", identity_ids=["identity-b"]
        )
    assert len({get_row.id, other_method.id, other_identity.id}) == 3


async def test_failed_registration_does_not_roll_back_committed_observation(
    database, unique_name
):
    context = await context_for(database, unique_name)
    session_id = context.toolbox.session_id
    url = "http://example.test/api/orders"
    started = asyncio.Event()

    async def good() -> None:
        await asyncio.sleep(0)
        async with database.transaction() as db:
            await record_asset(db, session_id=session_id, url=url, method="POST")
            await record_observation_ref(
                db,
                session_id=session_id,
                summary="已提交的观察",
                artifact_ids=[],
                source_run_id=context.toolbox.run_id,
            )
        started.set()

    async def bad() -> None:
        await asyncio.sleep(0)
        async with database.transaction() as db:
            await record_asset(db, session_id=session_id, url=url, method="POST")
            raise RuntimeError("模拟登记失败")

    await asyncio.gather(good(), bad(), return_exceptions=True)
    async with database.session() as db:
        observations = (
            await db.execute(
                select(func.count())
                .select_from(ObservationRow)
                .where(ObservationRow.session_id == session_id)
            )
        ).scalar_one()
        assets = (
            await db.execute(
                select(func.count()).select_from(AssetRow).where(AssetRow.session_id == session_id)
            )
        ).scalar_one()
    assert observations == 1, "另一个事务的失败不得撤销已经提交的观察"
    assert assets == 1


async def test_replay_returns_original_receipt_and_explicit_retest_is_new(database, unique_name):
    context = await context_for(database, unique_name)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="lead-parallel",
        store=None,
    )
    fn = cast(Any, record_lead).coroutine
    first = json.loads(await fn(runtime, "并行同一入口", "lead-parallel", url="http://a.test/x"))
    repeated = json.loads(await fn(runtime, "并行同一入口", "lead-parallel", url="http://a.test/x"))
    assert repeated == first
    await fn(runtime, "并行同一入口", "lead-parallel-2", url="http://a.test/x")
    async with database.session() as db:
        leads = (
            await db.execute(
                select(func.count())
                .select_from(LeadRow)
                .where(LeadRow.session_id == context.toolbox.session_id)
            )
        ).scalar_one()
        operations = (
            await db.execute(
                select(func.count())
                .select_from(ToolOperationRow)
                .where(ToolOperationRow.run_id == context.toolbox.run_id)
            )
        ).scalar_one()
    assert leads == 2 and operations == 2, "重放不产生新行；显式重测产生新的逻辑操作身份"
    assert first["evidence_source_ambiguous"] is False


async def test_url_without_collection_method_does_not_fake_a_get_asset(database, unique_name):
    context = await context_for(database, unique_name)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=f"lead-no-method-{uuid4().hex}",
        store=None,
    )
    fn = cast(Any, record_lead).coroutine
    payload = json.loads(
        await fn(runtime, "只有 URL 的假设", f"lead-no-method-{uuid4().hex}", url="http://a.test/y")
    )
    assert payload["asset_note"], "缺少采集方法时必须给出明确说明"
    async with database.session() as db:
        assets = (
            await db.execute(
                select(func.count()).select_from(AssetRow).where(AssetRow.session_id == context.toolbox.session_id)
            )
        ).scalar_one()
    assert assets == 0, "只提供 URL 时不得伪装成已验证的 GET 端点资产"
