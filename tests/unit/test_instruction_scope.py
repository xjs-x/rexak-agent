from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
from agent_workbench.domain.instruction_scope import instruction_hosts, scope_from_instructions
from agent_workbench.domain.scope import ScopePolicy


def test_user_target_enables_same_host_services_without_manual_scope():
    scope = scope_from_instructions(
        ScopeSnapshot(mode="instruction"), ["测试 http://lab.example.test/ 的安全性"]
    )
    policy = ScopePolicy(scope)
    assert policy.is_allowed("http://lab.example.test/")
    assert policy.is_allowed("https://lab.example.test:8443/")
    assert policy.is_allowed("tcp://lab.example.test:22/")
    assert not policy.is_allowed("http://external.example.test/")
    assert len(scope_from_instructions(scope, ["再测试 http://lab.example.test:8080/"]).rules) == 1


def test_strict_mode_preserves_protocol_and_port():
    scope = ScopeSnapshot(mode="scoped", rules=[ScopeRule(scheme="http", host="lab.test", port=80)])
    updated = scope_from_instructions(scope, ["测试 https://other.test:443/"])
    assert updated == scope
    assert not ScopePolicy(updated).is_allowed("http://lab.test:8080/")


def test_explicit_bare_endpoint_is_a_user_target_without_granting_other_hosts():
    texts = ["目标地址：node5.example.test:27571/\n\n授权 CTF 测试"]
    scope = scope_from_instructions(ScopeSnapshot(mode="instruction"), texts)
    assert instruction_hosts(texts) == ["node5.example.test"]
    assert ScopePolicy(scope).is_allowed("http://node5.example.test:27571/")
    assert ScopePolicy(scope).is_allowed("https://node5.example.test:27571/")
    assert not ScopePolicy(scope).is_allowed("http://other.example.test:27571/")
    assert instruction_hosts(["node5.example.test:27571/"]) == ["node5.example.test"]
    assert instruction_hosts(["不要访问 bad.example.test:1234/", "例如 demo.example.test:1234/",
                              "错误位置 code.py:123，另有 foo.test:1234", "目标：a.test:65536/",
                              "目标：a.test:0/", "联系 me@a.test:1234"]) == []
    strict = ScopeSnapshot(mode="scoped")
    assert scope_from_instructions(strict, texts) == strict


def test_targets_are_normalized_but_examples_and_exclusions_are_not_added():
    hosts = instruction_hosts(
        [
            "测试 http://LAB.test/，还有 10.0.0.4。不要访问 http://excluded.test/。例如 https://example.test/。测试 http://[::1]:8080/"
        ]
    )
    assert hosts == ["lab.test", "10.0.0.4", "::1"]
    assert (
        instruction_hosts(["你好", "http://?", "999.0.0.1", "测试 http://169.254.169.254/"]) == []
    )


def test_automatic_rules_use_existing_executor_protocol_without_expanding_other_hosts():
    from types import SimpleNamespace

    import pytest
    from workbench_executor.api import ScopePayload, _build_policy
    from workbench_executor.policy import PolicyDenied
    from workbench_executor.settings import ExecutorSettings

    from agent_workbench.tools.http import _scope_payload

    scope = scope_from_instructions(
        ScopeSnapshot(mode="instruction"), ["测试 http://lab.example.test/"]
    )
    wire = _scope_payload(SimpleNamespace(scope=ScopePolicy(scope)))  # type: ignore[arg-type]
    executor_scope = ScopePayload.model_validate(wire)
    policy = _build_policy(executor_scope, ExecutorSettings())
    for url in (
        "http://lab.example.test/",
        "https://lab.example.test:8443/",
        "tcp://lab.example.test:22/",
    ):
        assert policy.check(url) == url
    with pytest.raises(PolicyDenied):
        policy.check("http://outside.example.test/")
