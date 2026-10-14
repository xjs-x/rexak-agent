"""弱点协议与关系投影的纯逻辑边界（不需要数据库）。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent_workbench.agent.middleware.context_builder import _copy_working, _trim_working
from agent_workbench.contracts.weaknesses import (
    ActionabilityAssessment,
    ApplicabilitySpec,
    ConnectionReview,
    ProvidedCondition,
    RequiredConditionRef,
    actionability_basis_fingerprint,
    canonical_condition_key,
    connection_basis_fingerprint,
    content_revision_payload,
)


def test_condition_key_is_stable_and_content_sensitive() -> None:
    first = canonical_condition_key("响应头包含  X-Powered-By ")
    second = canonical_condition_key("响应头包含 X-Powered-By")
    assert first == second, "大小写与首尾空白差异不应产生新的条件身份"
    assert canonical_condition_key("响应头包含 X-Powered-By") != canonical_condition_key(
        "响应头包含 Server"
    )
    assert canonical_condition_key("同一陈述", seed="a") != canonical_condition_key(
        "同一陈述", seed="b"
    )


def test_content_revision_excludes_connection_review_metadata() -> None:
    base = {
        "title": "多余信息",
        "description": "错误页回显内部路径",
        "category": "info_disclosure",
        "weakness_kind": "enabling_condition",
        "applicability": {"asset_ids": [], "identity_ids": []},
        "provided_conditions": [{"key": "c1", "statement": "错误页回显路径", "limitations": None}],
    }
    assert content_revision_payload(**base) == content_revision_payload(
        **{**base, "provided_conditions": [dict(base["provided_conditions"][0])]}
    )
    changed = content_revision_payload(
        **{
            **base,
            "provided_conditions": [{"key": "c1", "statement": "错误页回显完整堆栈"}],
        }
    )
    assert changed != content_revision_payload(**base)


def test_connection_review_fingerprint_ignores_its_own_fields() -> None:
    """用途判断本身不能让自己过期：指纹不含判断字段。"""
    fingerprint = connection_basis_fingerprint(
        weakness_id="w1",
        content_revision=3,
        content_hash="abc",
        lead_ids=["l2", "l1"],
        condition_keys=["c1"],
    )
    same = connection_basis_fingerprint(
        weakness_id="w1",
        content_revision=3,
        content_hash="abc",
        lead_ids=["l1", "l2"],
        condition_keys=["c1"],
    )
    assert fingerprint == same, "顺序变化不算实质变化"
    assert fingerprint != connection_basis_fingerprint(
        weakness_id="w1",
        content_revision=4,
        content_hash="abc",
        lead_ids=["l1", "l2"],
        condition_keys=["c1"],
    )


def test_actionability_fingerprint_covers_conditions_and_source_versions() -> None:
    common: dict[str, Any] = {
        "hypothesis": "可能缺少权限校验",
        "next_action": "用低权账号重放请求",
        "required_conditions": [{"condition_key": "c1", "description": "低权账号", "satisfied": False}],
        "basis_revisions": [("w1", 2)],
        "identity_state": "anon",
    }
    fingerprint = actionability_basis_fingerprint(**common)
    assert fingerprint != actionability_basis_fingerprint(**{**common, "basis_revisions": [("w1", 3)]})
    assert fingerprint != actionability_basis_fingerprint(
        **{
            **common,
            "required_conditions": [
                {"condition_key": "c1", "description": "低权账号", "satisfied": True}
            ],
        }
    )
    assert fingerprint == actionability_basis_fingerprint(**dict(common))


def test_contracts_reject_missing_evidence_shaped_inputs() -> None:
    with pytest.raises(ValidationError):
        ProvidedCondition(key="c1", statement="   ")
    with pytest.raises(ValidationError):
        ActionabilityAssessment.model_validate({"state": "whatever", "note": "x"})
    with pytest.raises(ValidationError):
        RequiredConditionRef(description="")
    review = ConnectionReview(linked_lead_ids=[], no_connection_note="")
    assert review.has_judgment() is False, "空话不算判断"


def test_applicability_reuses_existing_identifiers_and_defaults() -> None:
    spec = ApplicabilitySpec.normalize(None)
    assert spec.asset_ids == [] and spec.identity_ids == []
    spec = ApplicabilitySpec.normalize(
        {"asset_ids": ["a1"], "identity_ids": ["i1"], "prerequisites": ["已登录"]}
    )
    assert spec.asset_ids == ["a1"] and spec.prerequisites == ["已登录"]


def test_working_context_trimming_drops_whole_objects() -> None:
    working = {
        "weakness_consumption": {
            "changed_weaknesses": [
                {"weakness_id": "w1"},
                {"weakness_id": "w2"},
            ],
            "related_leads": [{"lead_id": "l1"}, {"lead_id": "l2"}],
        }
    }
    copy = _copy_working(working)
    assert copy is not None and copy is not working
    assert _trim_working(copy) == "weakness:w2"
    assert _trim_working(copy) == "weakness:w1"
    assert _trim_working(copy) == "lead:l2"
    assert _trim_working(copy) == "lead:l1"
    assert _trim_working(copy) is None, "裁空后必须停止，不能无限循环"
    assert len(working["weakness_consumption"]["changed_weaknesses"]) == 2, "原视图不被修改"


def test_copy_working_handles_missing_or_non_dict_views() -> None:
    assert _copy_working(None) is None
    assert _trim_working({}) is None
