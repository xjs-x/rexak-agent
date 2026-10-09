"""范围策略（控制端模型与预检）。

**控制端不做最终放行判断**：真正的执行环境级检查在 Executor（`policy.py`）。
本模块负责：
- 规范化与校验用户声明的范围，拒绝含糊条目；
- 生成不可变 revision，供 `tool_operations.scope_revision` 记录；
- 提供"这条 URL 是否在声明范围内"的**预检**，用于提前给出清楚原因，
  但**不得**因此声称"范围强制保护已生效"（§10）。

反例：旧实现会归一化掉协议/端口后放行所有服务。这里**必须**把 scheme/host/port
作为范围的一部分显式保留。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from agent_workbench.contracts.identity import ScopeSnapshot


class ScopeViolation(Exception):
    def __init__(self, reason: str, *, target: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.target = target


@dataclass(frozen=True, slots=True)
class ParsedTarget:
    scheme: str
    host: str
    port: int
    path: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


DEFAULT_PORTS = {"http": 80, "https": 443}

#: 非目标管理面：HTTP 工具不能访问控制端管理接口、Executor 管理端口和云元数据（§10）
METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def parse_target(url: str) -> ParsedTarget:
    parts = urlsplit(url if "://" in url else f"http://{url}")
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ScopeViolation("目标缺少主机名", target=url)
    port = parts.port or DEFAULT_PORTS.get(scheme, 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return ParsedTarget(scheme=scheme, host=host, port=int(port), path=path)


def _host_matches(rule_host: str, host: str, include_subdomains: bool) -> bool:
    if rule_host == host:
        return True
    if include_subdomains:
        return host.endswith("." + rule_host)
    return False


def _is_private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


class ScopePolicy:
    """一次任务固定的范围策略。"""

    def __init__(self, snapshot: ScopeSnapshot) -> None:
        self._snapshot = snapshot
        self._rules = tuple(snapshot.rules)

    @property
    def revision(self) -> int:
        return self._snapshot.revision

    @property
    def snapshot(self) -> ScopeSnapshot:
        return self._snapshot

    def check_url(self, url: str) -> ParsedTarget:
        """预检一个 URL。不通过则抛 `ScopeViolation`。

        逐跳检查由调用方对每个重定向目标重复调用本方法（§10）。
        """
        target = parse_target(url)

        if target.host in METADATA_HOSTS or target.host in self._snapshot.denied_metadata_hosts:
            raise ScopeViolation("目标是云元数据地址，属于非目标管理面", target=url)

        # 回环地址本身允许（本地测试需求），但执行岛的回环不是控制机的回环；
        # 调用方应通过 `loopback_requires_mapping` 提示需要显式目标映射（§9.3）。
        for rule in self._rules:
            if rule.scheme not in {"any", target.scheme}:
                continue
            if rule.port is not None and rule.port != target.port:
                continue
            if not _host_matches(rule.host, target.host, rule.include_subdomains):
                continue
            if _is_private(target.host) and not rule.allow_private:
                raise ScopeViolation("目标是私网地址且规则未允许私网", target=url)
            return target

        raise ScopeViolation(
            "目标不在声明的测试范围内（范围按 scheme/host/port 匹配，不做端口剥离）",
            target=url,
        )

    def is_allowed(self, url: str) -> bool:
        try:
            self.check_url(url)
        except ScopeViolation:
            return False
        return True

    def loopback_requires_mapping(self, url: str) -> bool:
        """执行岛回环 ≠ 控制机回环。返回 True 表示必须依赖显式目标映射。"""
        return parse_target(url).host in _LOOPBACK_HOSTS


class ScopeDeniedError(Exception):
    """供上层统一映射为 `ErrorCode.SCOPE_DENIED`。"""


def validate_scope(snapshot: ScopeSnapshot) -> None:
    """拒绝含糊条目：空规则、无端口的 `any`+包含子域组合会放大范围，要求显式。"""
    if not snapshot.rules:
        raise ValueError("范围必须至少包含一条规则；不允许空范围表示全放行")
    for index, rule in enumerate(snapshot.rules):
        if rule.scheme == "any" and rule.port is None:
            raise ValueError(
                f"第 {index + 1} 条规则缺少端口约束：'any' 协议 + 任意端口会放行所有服务；"
                "请显式声明 scheme 或 port"
            )
        if rule.include_subdomains and rule.host.count(".") == 0:
            raise ValueError(
                f"第 {index + 1} 条规则对裸域 {rule.host!r} 开启子域包含，"
                "会同时匹配第三方域名；请改写为明确的子域规则"
            )
    if any(rule.host in METADATA_HOSTS for rule in snapshot.rules):
        raise ValueError("范围规则不得包含云元数据地址")
