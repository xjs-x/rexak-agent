"""P1-C：typed assets / parameters / coverage v2 约束。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_workbench.contracts.enums import CoverageStatus
from agent_workbench.domain.findings import CoverageService
from agent_workbench.persistence.tables import (
    AssetRow,
    CoverageRow,
    ParameterRow,
    RequestVariantRow,
)
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


def _asset(context, **extra):
    now = datetime.now(UTC)
    values = {
        "session_id": context.toolbox.session_id,
        "scheme": "http",
        "host": "example.test",
        "port": 80,
        "path": "/a?x=1",
        "method": "GET",
        "query_keys": ["x"],
        "first_seen_at": now,
        "last_seen_at": now,
        "identity_refs": [],
        "identity_key": "",
        "record_role": "canonical",
        "path_component": "/a",
        "method_token": "GET",
        "normalizer_version": "v1",
    }
    values.update(extra)
    return AssetRow(**values)


async def test_canonical_endpoint_unique_and_alias_kept(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        canonical = _asset(context)
        db.add(canonical)
        await db.flush()
        alias = _asset(
            context,
            record_role="alias",
            canonical_asset_id=canonical.id,
        )
        db.add(alias)
        await db.flush()
        assert alias.canonical_asset_id == canonical.id
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(_asset(context))
                await db.flush()


async def test_request_variant_and_parameter_instances(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        asset = _asset(context)
        db.add(asset)
        await db.flush()
        db.add(
            RequestVariantRow(
                session_id=context.toolbox.session_id,
                asset_id=asset.id,
                path="/a",
                query_string="x=1&x=2",
                has_query_delimiter=True,
                method_token="GET",
                normalizer_version="v1",
                source_kind="legacy_structured",
            )
        )
        db.add(
            ParameterRow(
                session_id=context.toolbox.session_id,
                asset_id=asset.id,
                location="query",
                name="x",
                occurrence=0,
                normalizer_version="v1",
            )
        )
        db.add(
            ParameterRow(
                session_id=context.toolbox.session_id,
                asset_id=asset.id,
                location="query",
                name="x",
                occurrence=1,
                normalizer_version="v1",
            )
        )
        await db.flush()
    async with database.session() as db:
        params = (
            await db.execute(
                select(ParameterRow).where(
                    ParameterRow.session_id == context.toolbox.session_id
                )
            )
        ).scalars().all()
    assert len(params) == 2


async def test_coverage_cell_v2_null_parameter_is_not_distinct(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        asset = _asset(context)
        db.add(asset)
        await db.flush()
        base = {
            "session_id": context.toolbox.session_id,
            "asset_id": asset.id,
            "entry_label": "/a-v2",
            "identity_id": None,
            "check_category": "configuration",
            "status": "not_checked",
            "freshness": "current",
            "resolution_status": "resolved",
            "profile_id": "web",
            "profile_version": "v1",
            "condition_fingerprint": "f" * 64,
        }
        db.add(CoverageRow(**base))
        await db.flush()
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(CoverageRow(**base))
                await db.flush()


async def test_coverage_service_v2_numerator_and_denominator(database, unique_name):
    context = await context_for(database, unique_name)
    async with database.transaction() as db:
        asset = _asset(context)
        db.add(asset)
        await db.flush()
        service = CoverageService(db)
        await service.record(
            session_id=context.toolbox.session_id,
            asset_id=asset.id,
            entry_label="/a",
            check_category="configuration",
            status=CoverageStatus.CHECKED_NEGATIVE,
            profile_id="web",
            profile_version="v1",
            condition_fingerprint="f" * 64,
            evidence_refs=["e1"],
        )
        await service.record(
            session_id=context.toolbox.session_id,
            asset_id=asset.id,
            entry_label="/a",
            check_category="authentication",
            status=CoverageStatus.NOT_CHECKED,
            profile_id="web",
            profile_version="v1",
            condition_fingerprint="f" * 64,
        )
        await service.record(
            session_id=context.toolbox.session_id,
            asset_id=asset.id,
            entry_label="/a",
            check_category="headers",
            status=CoverageStatus.NOT_APPLICABLE,
            profile_id="web",
            profile_version="v1",
            condition_fingerprint="f" * 64,
            evidence_refs=["e2"],
        )
        summary = await service.summary(context.toolbox.session_id)
    v2 = summary["coverage_v2"]
    assert v2["total_cells"] == 3
    assert v2["applicable_cells"] == 2
    assert v2["checked_cells"] == 1
    assert v2["checked_ratio"] == 0.5
