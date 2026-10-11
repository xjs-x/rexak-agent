"""价格目录必须版本化，编辑不能重算历史模型调用。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from agent_workbench.api.app import create_app
from agent_workbench.config.catalog import PriceCatalog
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.prices import (
    PriceUpdate,
    current_price_catalog,
    price_catalog_for_run,
    update_price_catalog,
)
from agent_workbench.observability.costs import compute_cost, normalize_usage
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import ModelCallRow, PriceCatalogVersionRow, RunRow

pytestmark = pytest.mark.integration


def _update(version: str) -> PriceUpdate:
    return PriceUpdate(
        expected_version=version,
        provider_id="xiaomi",
        model_name="mimo-v2.6-pro",
        currency="CNY",
        input_per_1m=Decimal("1.25"),
        output_per_1m=Decimal("5.00"),
        cache_read_per_1m=Decimal("0.25"),
        cache_tokens_included_in_input=True,
    )


async def test_price_update_is_versioned_and_rejects_stale_write(database) -> None:
    async with database.session() as db:
        original = await current_price_catalog(db)
        updated = await update_price_catalog(db, _update(original.version))
        assert updated.version != original.version
        price = updated.find("xiaomi", "mimo-v2.6-pro")
        assert price is not None
        assert price.input_per_unit == Decimal("1.25")
        assert price.cache_tokens_included_in_input is True
        cost = compute_cost(
            normalize_usage(
                {
                    "input_tokens": 2_000_000,
                    "output_tokens": 1_000_000,
                    "input_token_details": {"cache_read": 1_000_000},
                }
            ),
            price,
            updated,
        )
        assert cost.amount == Decimal("6.5000000000")
        assert cost.snapshot is not None and cost.snapshot.catalog_version == updated.version
        assert (await current_price_catalog(db)).version == updated.version
        history = (
            (
                await db.execute(
                    select(PriceCatalogVersionRow).where(
                        PriceCatalogVersionRow.version.in_([original.version, updated.version])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(history) == 2
        assert len([row for row in history if row.is_current]) == 1
        with pytest.raises(ApiError, match="版本已变化"):
            await update_price_catalog(db, _update(original.version))
        await db.rollback()

    async with database.session() as db:
        assert (await current_price_catalog(db)).version == original.version


async def test_existing_run_keeps_price_version_after_catalog_changes(
    database, unique_name
) -> None:
    async with database.session() as db:
        project = await ProjectRepository(db).create(
            owner_id="price-test",
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="价格版本测试",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = RunRow(
            id=uuid4(),
            session_id=session.id,
            status="paused",
            objective="验证价格版本",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
        )
        db.add(run)
        await db.flush()
        original = await price_catalog_for_run(db, run.id)
        assert run.price_catalog_version == original.version
        updated = await update_price_catalog(db, _update(original.version))
        assert updated.version != original.version
        resumed = await price_catalog_for_run(db, run.id)
        assert resumed.version == original.version
        assert run.price_catalog_version == original.version
        await db.rollback()


async def test_unpinned_run_with_old_model_call_does_not_take_new_price(
    database, unique_name
) -> None:
    async with database.session() as db:
        project = await ProjectRepository(db).create(
            owner_id="price-test",
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="旧运行价格测试",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = RunRow(
            id=uuid4(),
            session_id=session.id,
            status="paused",
            objective="旧运行",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
        )
        db.add(run)
        db.add(
            ModelCallRow(
                model_call_id=uuid4(),
                logical_call_id=uuid4(),
                run_id=run.id,
                session_id=session.id,
                phase="model_complete",
                provider_id="xiaomi",
                model_name="mimo-v2.6-pro",
                price_snapshot={},
            )
        )
        await db.flush()
        with pytest.raises(RuntimeError, match="缺少可恢复的价格版本"):
            await price_catalog_for_run(db, run.id)
        await db.rollback()


def test_price_update_rejects_invalid_amount_and_currency() -> None:
    with pytest.raises(ValidationError):
        PriceUpdate.model_validate({**_update("v1").model_dump(), "input_per_1m": "-1"})
    with pytest.raises(ValidationError):
        PriceUpdate.model_validate({**_update("v1").model_dump(), "currency": "usd"})


async def test_price_api_is_readable_and_requires_csrf_for_write(database, monkeypatch) -> None:
    async with database.session() as db:
        original = await current_price_catalog(db)
    calls: list[PriceUpdate] = []

    async def fake_update(_db, payload: PriceUpdate) -> PriceCatalog:
        calls.append(payload)
        return PriceCatalog(version="api-test", prices=(payload.to_model_price(),))

    monkeypatch.setattr("agent_workbench.domain.prices.update_price_catalog", fake_update)
    with TestClient(create_app()) as client:
        ticket = client.post("/api/session")
        assert ticket.status_code == 200
        prices = client.get("/api/settings/prices")
        assert prices.status_code == 200
        assert prices.json()["version"] == original.version
        settings = client.get("/api/settings/models")
        assert settings.status_code == 200
        assert settings.json()["price_catalog_version"] == original.version
        body = _update(original.version).model_dump(mode="json")
        assert client.post("/api/settings/prices", json=body).status_code == 403
        saved = client.post(
            "/api/settings/prices",
            json=body,
            headers={"X-Workbench-CSRF": ticket.json()["csrf_token"]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == "api-test"
        assert len(calls) == 1
