"""P0 验证 recipe 的确定性边界。"""

from __future__ import annotations

from agent_workbench.domain.verification_recipes import (
    RecipeOutcome,
    evaluate_recipe,
    parse_checks,
)


def _check(artifact_id: str, role: str, **extra):
    base = {"artifact_id": artifact_id, "role": role, "must_match": [], "must_not_match": []}
    base.update(extra)
    return base


def test_empty_criteria_and_missing_control_are_invalid():
    checks = [
        _check("a", "initial"),
        _check("b", "recheck"),
        _check("c", "control"),
    ]
    result = evaluate_recipe(
        "command_echo", checks, {"a": b"uid=48", "b": b"uid=48", "c": b"no"},
        claim_scope={"identity": "apache", "host": "phineas"},
    )
    assert result.outcome is RecipeOutcome.INVALID_EVIDENCE


def test_control_positive_is_inconclusive_not_fail():
    checks = [
        _check("a", "initial", must_match=[r"uid=48"]),
        _check("b", "recheck", must_match=[r"uid=48"]),
        _check("c", "control", must_not_match=[r"uid=48"]),
    ]
    result = evaluate_recipe(
        "command_echo", checks,
        {"a": b"uid=48", "b": b"uid=48", "c": b"uid=48"},
        claim_scope={"identity": "apache", "host": "phineas"},
    )
    assert result.outcome is RecipeOutcome.INCONCLUSIVE


def test_hash_mismatch_is_invalid_evidence():
    checks = [
        _check("a", "initial", must_match=[r"uid=48"], sha256="0" * 64),
        _check("b", "recheck", must_match=[r"uid=48"]),
        _check("c", "control", must_not_match=[r"uid=48"]),
    ]
    result = evaluate_recipe(
        "command_echo", checks,
        {"a": b"uid=48", "b": b"uid=48", "c": b"no"},
        claim_scope={"identity": "apache", "host": "phineas"},
    )
    assert result.outcome is RecipeOutcome.INVALID_EVIDENCE


def test_sentinel_requires_reference_hash_and_control():
    checks = [
        _check("a", "initial", sha256="a" * 64),
        _check("b", "recheck", sha256="a" * 64),
        _check("c", "control", must_not_match=[r"sentinel"]),
    ]
    result = evaluate_recipe(
        "sentinel_read", checks, {"a": b"sentinel", "b": b"sentinel", "c": b"other"}
    )
    assert result.outcome is RecipeOutcome.INVALID_EVIDENCE


def test_identity_boundary_requires_identity_and_same_request_digest():
    checks = [
        _check("a", "anonymous", must_match=[r"denied"], identity_ref="anonymous", request_digest="d1"),
        _check("b", "authenticated", must_match=[r"ok"], identity_ref="user:1", request_digest="d2"),
        _check("c", "control", must_not_match=[r"ok"]),
    ]
    result = evaluate_recipe(
        "identity_boundary", checks, {"a": b"denied", "b": b"ok", "c": b"no"}
    )
    assert result.outcome is RecipeOutcome.INVALID_EVIDENCE


def test_public_read_requires_anonymous_flag():
    checks = [
        _check("a", "initial", must_match=[r"public"]),
        _check("b", "recheck", must_match=[r"public"]),
        _check("c", "control", must_not_match=[r"public"]),
    ]
    result = evaluate_recipe("public_read", checks, {"a": b"public", "b": b"public", "c": b"no"})
    assert result.outcome is RecipeOutcome.INVALID_EVIDENCE


def test_default_login_requires_post_logout_check():
    checks = [
        _check("a", "login", must_match=[r"welcome"]),
        _check("b", "session", must_match=[r"account"]),
        _check("c", "logout", must_match=[r"bye"]),
        _check("d", "control", must_not_match=[r"welcome"]),
    ]
    result = evaluate_recipe(
        "default_login", checks, {"a": b"welcome", "b": b"account", "c": b"bye", "d": b"no"}
    )
    assert result.outcome is RecipeOutcome.INVALID_EVIDENCE


def test_paired_difference_pass_fail_inconclusive():
    checks = [
        _check("a", "baseline", must_match=[r"."]),
        _check("b", "treatment", must_match=[r"."]),
    ]
    data = {"a": b"x", "b": b"x"}
    passed = evaluate_recipe(
        "paired_difference", checks, data,
        statistics_payload={
            "pairs": [
                {"baseline": 10, "treatment": 20},
                {"baseline": 11, "treatment": 21},
                {"baseline": 9, "treatment": 19},
                {"baseline": 10, "treatment": 20},
                {"baseline": 10, "treatment": 20},
            ],
            "delta": 5,
            "min_pairs": 5,
        },
    )
    assert passed.outcome is RecipeOutcome.PASS
    failed = evaluate_recipe(
        "paired_difference", checks, data,
        statistics_payload={
            "pairs": [
                {"baseline": 10, "treatment": 10.1},
                {"baseline": 11, "treatment": 10.9},
                {"baseline": 9, "treatment": 9.1},
                {"baseline": 10, "treatment": 10.0},
                {"baseline": 10, "treatment": 10.1},
            ],
            "delta": 5,
            "min_pairs": 5,
        },
    )
    assert failed.outcome is RecipeOutcome.FAIL
    inconclusive = evaluate_recipe(
        "paired_difference", checks, data,
        statistics_payload={"pairs": [{"baseline": 1, "treatment": 2}], "delta": 0.5, "min_pairs": 5},
    )
    assert inconclusive.outcome is RecipeOutcome.INCONCLUSIVE


def test_parse_checks_rejects_unknown_role():
    try:
        parse_checks([{"artifact_id": "a", "role": "unknown"}])
    except ValueError as exc:
        assert "role 非法" in str(exc)
    else:
        raise AssertionError("未知 role 必须拒绝")
