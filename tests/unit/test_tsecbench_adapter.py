"""评测适配层纯函数测试：配置、Flag 校验、目标与范围构建。"""

from __future__ import annotations

import pytest

from agent_workbench.tsecbench import config as config_module
from agent_workbench.tsecbench.config import ConfigError, load_config
from agent_workbench.tsecbench.flags import extract_flag_candidates, mask_flag
from agent_workbench.tsecbench.objective import (
    build_objective,
    build_scope,
    parse_address,
    parse_addresses,
)
from agent_workbench.tsecbench.platform import ChallengeInfo
from tests.support.settings import isolated_settings


def _settings():
    return isolated_settings(
        access_token="test-access-token",
        api_host="127.0.0.1",
        api_port=8770,
        history_assistance_mode="memory",
    )


def _write_env(tmp_path, body: str):
    path = tmp_path / "tsecbench.env"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def test_config_reads_private_file_and_redacts_token(tmp_path, monkeypatch):
    for name in ("BENCHMARK_TOKEN", "BENCHMARK_BASE_URL", "TSECBENCH_MAX_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_module, "get_settings", _settings)
    path = _write_env(
        tmp_path,
        "BENCHMARK_TOKEN=super-secret-token-value\n"
        "BENCHMARK_BASE_URL=https://benchmark.example.com/\n"
        "TSECBENCH_MAX_SECONDS=120\n",
    )

    config = load_config(env_file=path)

    assert config.token == "super-secret-token-value"
    assert config.base_url == "https://benchmark.example.com"
    assert config.max_seconds == 120
    assert config.api_token == "test-access-token"
    assert config.api_base_url == "http://127.0.0.1:8770"
    # 令牌不得出现在 repr / model_dump / describe 中
    assert "super-secret-token-value" not in repr(config)
    assert "super-secret-token-value" not in str(config.model_dump())
    assert "super-secret-token-value" not in str(config.describe())
    assert config.token_fingerprint and len(config.token_fingerprint) == 8


def test_config_environment_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "get_settings", _settings)
    monkeypatch.setenv("BENCHMARK_TOKEN", "from-env")
    monkeypatch.setenv("BENCHMARK_BASE_URL", "https://env.example.com")
    path = _write_env(
        tmp_path, "BENCHMARK_TOKEN=from-file\nBENCHMARK_BASE_URL=https://file.example.com\n"
    )

    config = load_config(env_file=path)

    assert config.token == "from-env"
    assert config.base_url == "https://env.example.com"


def test_config_requires_explicit_token_and_non_default_api_token(tmp_path, monkeypatch):
    for name in ("BENCHMARK_TOKEN", "BENCHMARK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_module, "get_settings", _settings)
    path = _write_env(tmp_path, "BENCHMARK_BASE_URL=https://benchmark.example.com\n")

    config = load_config(env_file=path)
    with pytest.raises(ConfigError, match="BENCHMARK_TOKEN"):
        config.require_ready()

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: isolated_settings(
            access_token="change-me-local-access-token",
            api_host="127.0.0.1",
            api_port=8770,
        ),
    )
    config = load_config(env_file=path)
    config = config.model_copy(update={"token": "present"})
    with pytest.raises(ConfigError, match="WORKBENCH_ACCESS_TOKEN"):
        config.require_ready()


def test_config_rejects_non_loopback_api_base(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "get_settings", _settings)
    path = _write_env(
        tmp_path,
        "BENCHMARK_TOKEN=t\n"
        "BENCHMARK_BASE_URL=https://benchmark.example.com\n"
        "WORKBENCH_API_URL=http://10.0.0.5:8770\n",
    )
    with pytest.raises(ConfigError, match="回环地址"):
        load_config(env_file=path)


def test_flag_extraction_multiple_and_dedup():
    text = """
结论如下：
flag{alpha-001}
另一个是 FLAG{Beta_2}
重复一次 flag{alpha-001}
"""
    flags = extract_flag_candidates(text)
    assert [item.value for item in flags] == ["flag{alpha-001}", "FLAG{Beta_2}"]


def test_flag_extraction_rejects_placeholders_and_malformed():
    text = "\n".join(
        [
            "格式为 flag{...}",
            "示例：flag{your_flag_here}",
            "flag{has space}",
            "flag{a<b}",
            "flag{xxxx}",
            "真实：flag{real_value_1}",
        ]
    )
    assert [item.value for item in extract_flag_candidates(text)] == ["flag{real_value_1}"]


def test_flag_extraction_caps_candidates():
    text = "\n".join(f"flag{{value_{index}}}" for index in range(30))
    assert len(extract_flag_candidates(text, max_candidates=5)) == 5


def test_mask_flag_keeps_only_edges():
    assert "middle" not in mask_flag("flag{very-middle-secret}")


def test_parse_address_and_scope():
    targets = parse_addresses(["10.1.2.3:8080", "10.1.2.3:8080", "[2001:db8::1]:443"])
    assert [item.origin for item in targets] == ["10.1.2.3:8080", "[2001:db8::1]:443"]

    scope = build_scope(targets, "web-001")
    assert scope.mode == "scoped"
    assert [(rule.host, rule.port) for rule in scope.rules] == [
        ("10.1.2.3", 8080),
        ("2001:db8::1", 443),
    ]
    assert all(rule.scheme == "any" and rule.allow_private for rule in scope.rules)
    assert "web-001" in str(scope.rules[0].notes)


@pytest.mark.parametrize("raw", ["10.0.0.1", "2001:db8::1", ""])
def test_parse_address_rejects_ambiguous_input(raw):
    with pytest.raises(ValueError):
        parse_address(raw)


def _challenge(**overrides) -> ChallengeInfo:
    base = {
        "unique_code": "web-001",
        "description": "目标是一个内部 CMS，请获取后台 flag。",
        "difficulty": "medium",
        "level": "2",
        "total_score": 300,
        "flag_count": 2,
        "correct_flag_count": 0,
        "is_completed": False,
        "container_status": "available",
        "container_addr": ("10.1.2.3:8080",),
    }
    return ChallengeInfo(**{**base, **overrides})


def test_build_objective_mentions_all_constraints():
    challenge = _challenge()
    targets = parse_addresses(list(challenge.container_addr))
    objective = build_objective(challenge, targets, max_seconds=1800)

    assert "web-001" in objective
    assert "10.1.2.3:8080" in objective
    assert "flag 总数：2" in objective
    assert "单独一行" in objective
    assert "禁止扫描 VPN 网段内其它资产" in objective
    assert "30 分钟" in objective


def test_build_objective_truncates_long_description():
    challenge = _challenge(description="A" * 30_000)
    objective = build_objective(challenge, parse_addresses(["10.1.2.3:8080"]), max_seconds=60)
    assert "题目描述已截断" in objective
    assert len(objective) < 30_000
