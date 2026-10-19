"""coverage 清单生成与对账：先冻结 Uᵥ，再计算分子/分母。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CoverageProfile:
    profile_id: str
    profile_version: str
    check_categories: tuple[str, ...]
    include_parameter_cells: bool = True
    include_anonymous: bool = True


@dataclass(frozen=True, slots=True)
class CoverageCell:
    asset_id: UUID
    parameter_id: UUID | None
    identity_id: UUID | None
    identity_revision: int | None
    check_category: str
    condition_fingerprint: str
    profile_id: str
    profile_version: str

    def key(self) -> tuple[Any, ...]:
        return (
            str(self.asset_id),
            str(self.parameter_id) if self.parameter_id else None,
            str(self.identity_id) if self.identity_id else None,
            self.identity_revision,
            self.check_category,
            self.condition_fingerprint,
            self.profile_id,
            self.profile_version,
        )


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _identity_pairs(identities: Sequence[Any]) -> list[tuple[UUID | None, int | None]]:
    pairs: list[tuple[UUID | None, int | None]] = []
    for identity in identities:
        identity_id = _value(identity, "id", None)
        revision = _value(identity, "auth_revision", None)
        if identity_id is None:
            continue
        pairs.append((identity_id, int(revision) if revision is not None else None))
    return pairs


def build_checklist(
    *,
    profile: CoverageProfile,
    assets: Sequence[Any],
    parameters: Sequence[Any],
    identities: Sequence[Any],
    condition_fingerprint: str,
) -> list[CoverageCell]:
    """按冻结 profile 生成有限 cell 集合；不把缺失记录当成 not_applicable。"""
    if not condition_fingerprint:
        raise ValueError("condition_fingerprint 不能为空")
    canonical_assets = [
        asset for asset in assets if _value(asset, "record_role", "canonical") == "canonical"
    ]
    identity_pairs = _identity_pairs(identities)
    if profile.include_anonymous:
        identity_pairs.insert(0, (None, None))
    if not identity_pairs:
        raise ValueError("清单至少需要一个身份或 anonymous")

    params_by_asset: dict[UUID, list[Any]] = {}
    for parameter in parameters:
        asset_id = _value(parameter, "asset_id", None)
        if asset_id is not None:
            params_by_asset.setdefault(asset_id, []).append(parameter)

    cells: list[CoverageCell] = []
    for asset in canonical_assets:
        asset_id = _value(asset, "id", None)
        if asset_id is None:
            continue
        for identity_id, revision in identity_pairs:
            for category in profile.check_categories:
                cells.append(
                    CoverageCell(
                        asset_id=asset_id,
                        parameter_id=None,
                        identity_id=identity_id,
                        identity_revision=revision,
                        check_category=category,
                        condition_fingerprint=condition_fingerprint,
                        profile_id=profile.profile_id,
                        profile_version=profile.profile_version,
                    )
                )
                if not profile.include_parameter_cells:
                    continue
                for parameter in params_by_asset.get(asset_id, []):
                    parameter_id = _value(parameter, "parameter_id", None)
                    if parameter_id is None:
                        continue
                    cells.append(
                        CoverageCell(
                            asset_id=asset_id,
                            parameter_id=parameter_id,
                            identity_id=identity_id,
                            identity_revision=revision,
                            check_category=category,
                            condition_fingerprint=condition_fingerprint,
                            profile_id=profile.profile_id,
                            profile_version=profile.profile_version,
                        )
                    )
    unique = {cell.key(): cell for cell in cells}
    return sorted(
        unique.values(),
        key=lambda cell: tuple("" if item is None else str(item) for item in cell.key()),
    )


def checklist_signature(cells: Sequence[CoverageCell]) -> str:
    payload = [cell.key() for cell in cells]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def reconcile_checklist(
    cells: Sequence[CoverageCell],
    coverage_rows: Sequence[Any],
) -> dict[str, Any]:
    """按清单统计；缺失 cell 是 not_checked，不是 not_applicable。"""
    by_key: dict[tuple[Any, ...], Any] = {}
    for row in coverage_rows:
        key = (
            str(_value(row, "asset_id", "")) if _value(row, "asset_id", None) else None,
            str(_value(row, "parameter_id", "")) if _value(row, "parameter_id", None) else None,
            str(_value(row, "identity_id", "")) if _value(row, "identity_id", None) else None,
            _value(row, "identity_revision", None),
            _value(row, "check_category", ""),
            _value(row, "condition_fingerprint", None),
            _value(row, "profile_id", None),
            _value(row, "profile_version", None),
        )
        by_key[key] = row

    checked = 0
    not_applicable = 0
    missing: list[dict[str, Any]] = []
    for cell in cells:
        row = by_key.get(cell.key())
        if row is None:
            missing.append({"cell": cell.key(), "status": "not_checked"})
            continue
        status = str(_value(row, "status", "not_checked"))
        freshness = str(_value(row, "freshness", "current"))
        resolution = str(_value(row, "resolution_status", "resolved"))
        evidence = list(_value(row, "evidence_refs", []) or [])
        if status == "not_applicable" and evidence:
            not_applicable += 1
        elif status in {"checked_positive", "checked_negative"} and freshness == "current" and resolution == "resolved":
            checked += 1
    denominator = len(cells) - not_applicable
    return {
        "total_cells": len(cells),
        "applicable_cells": denominator,
        "checked_cells": checked,
        "checked_ratio": round(checked / denominator, 4) if denominator else None,
        "not_applicable_cells": not_applicable,
        "missing_cells": missing,
        "checklist_signature": checklist_signature(cells),
        "denominator_definition": (
            "分母 = 冻结清单 Uᵥ − 有证据的 not_applicable；缺失/blocked/not_checked/stale 留在分母。"
        ),
    }


__all__ = [
    "CoverageCell",
    "CoverageProfile",
    "build_checklist",
    "checklist_signature",
    "reconcile_checklist",
]
