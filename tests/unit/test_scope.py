"""范围策略的边界样本。

这些用例直接对应 BUILD_PLAN §10 与 ACCEPTANCE A21/A22 的意图：
**不能**像旧实现那样剥掉端口后放行所有服务。
"""

from __future__ import annotations

import pytest

from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
from agent_workbench.domain.scope import (
    ScopePolicy,
    ScopeViolation,
    parse_target,
    validate_scope,
)


def _policy(*rules: ScopeRule) -> ScopePolicy:
    return ScopePolicy(ScopeSnapshot(revision=1, rules=list(rules)))


def test_port_is_part_of_scope() -> None:
    """同主机不同端口是不同范围 —— 端口不能被剥离。"""
    policy = _policy(ScopeRule(scheme="http", host="127.0.0.1", port=8899))
    assert policy.is_allowed("http://127.0.0.1:8899/")
    assert not policy.is_allowed("http://127.0.0.1:8080/")
    assert not policy.is_allowed("http://127.0.0.1/")


def test_scheme_is_enforced_when_specified() -> None:
    policy = _policy(ScopeRule(scheme="https", host="example.test", port=443))
    assert policy.is_allowed("https://example.test/")
    assert not policy.is_allowed("http://example.test:443/")


def test_subdomain_rule_is_explicit() -> None:
    exact = _policy(ScopeRule(scheme="http", host="example.test", port=80))
    assert not exact.is_allowed("http://api.example.test/")

    wildcard = _policy(
        ScopeRule(scheme="http", host="example.test", port=80, include_subdomains=True)
    )
    assert wildcard.is_allowed("http://api.example.test/")
    assert not wildcard.is_allowed("http://notexample.test/")


def test_metadata_hosts_are_always_denied() -> None:
    policy = _policy(ScopeRule(scheme="any", host="169.254.169.254", port=None))
    with pytest.raises(ScopeViolation, match="云元数据"):
        policy.check_url("http://169.254.169.254/latest/meta-data/")


def test_private_target_allowed_only_when_rule_permits() -> None:
    strict = _policy(ScopeRule(scheme="http", host="10.0.0.5", port=80, allow_private=False))
    assert not strict.is_allowed("http://10.0.0.5/")

    local_ok = _policy(ScopeRule(scheme="http", host="10.0.0.5", port=80, allow_private=True))
    assert local_ok.is_allowed("http://10.0.0.5/")


def test_loopback_requires_explicit_mapping_flag() -> None:
    """执行岛的回环不是控制机的回环：必须能识别并提示需要显式映射。"""
    policy = _policy(ScopeRule(scheme="http", host="localhost", port=3000))
    assert policy.loopback_requires_mapping("http://localhost:3000/")
    assert not policy.loopback_requires_mapping("http://example.test:3000/")


def test_query_string_is_preserved_in_parsed_path() -> None:
    target = parse_target("http://example.test:80/a/b?id=1&id=2&x=%20")
    assert target.path == "/a/b?id=1&id=2&x=%20", "重复参数与编码差异必须保留在原件里"


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [ScopeRule(scheme="any", host="example.test", port=None)],
        [ScopeRule(scheme="http", host="test", port=80, include_subdomains=True)],
    ],
)
def test_validate_scope_rejects_overbroad_or_empty(rules: list[ScopeRule]) -> None:
    with pytest.raises(ValueError):
        validate_scope(ScopeSnapshot(rules=rules))


def test_validate_scope_accepts_explicit_rules() -> None:
    validate_scope(ScopeSnapshot(rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)]))
