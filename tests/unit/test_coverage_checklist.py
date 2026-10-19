"""P1：coverage 清单生成与对账。"""

from __future__ import annotations

from uuid import uuid4

from agent_workbench.domain.coverage_checklist import (
    CoverageProfile,
    build_checklist,
    reconcile_checklist,
)


def _asset(asset_id, role="canonical"):
    return {"id": asset_id, "record_role": role}


def _parameter(asset_id, parameter_id):
    return {"asset_id": asset_id, "parameter_id": parameter_id}


def test_checklist_covers_endpoint_and_parameter_cells():
    asset_id = uuid4()
    parameter_id = uuid4()
    cells = build_checklist(
        profile=CoverageProfile(
            profile_id="web",
            profile_version="v1",
            check_categories=("configuration",),
        ),
        assets=[_asset(asset_id), _asset(uuid4(), role="alias")],
        parameters=[_parameter(asset_id, parameter_id)],
        identities=[{"id": uuid4(), "auth_revision": 2}],
        condition_fingerprint="f" * 64,
    )
    assert len(cells) == 4  # anonymous endpoint/parameter + identity endpoint/parameter
    assert {cell.parameter_id for cell in cells} == {None, parameter_id}


def test_reconcile_counts_missing_as_denominator_and_not_applicable_excluded():
    asset_id = uuid4()
    cells = build_checklist(
        profile=CoverageProfile(
            profile_id="web", profile_version="v1", check_categories=("c1", "c2")
        ),
        assets=[_asset(asset_id)],
        parameters=[],
        identities=[],
        condition_fingerprint="f" * 64,
    )
    first, second = cells
    rows = [
        {
            "asset_id": first.asset_id,
            "parameter_id": None,
            "identity_id": None,
            "identity_revision": None,
            "check_category": first.check_category,
            "condition_fingerprint": first.condition_fingerprint,
            "profile_id": first.profile_id,
            "profile_version": first.profile_version,
            "status": "checked_negative",
            "freshness": "current",
            "resolution_status": "resolved",
            "evidence_refs": ["e1"],
        },
        {
            "asset_id": second.asset_id,
            "parameter_id": None,
            "identity_id": None,
            "identity_revision": None,
            "check_category": second.check_category,
            "condition_fingerprint": second.condition_fingerprint,
            "profile_id": second.profile_id,
            "profile_version": second.profile_version,
            "status": "not_applicable",
            "freshness": "current",
            "resolution_status": "resolved",
            "evidence_refs": ["e2"],
        },
    ]
    result = reconcile_checklist(cells, rows)
    assert result["total_cells"] == 2
    assert result["applicable_cells"] == 1
    assert result["checked_cells"] == 1
    assert result["checked_ratio"] == 1.0
    assert result["missing_cells"] == []
