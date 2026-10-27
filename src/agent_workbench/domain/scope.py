"""范围策略（控制端模型与预检）。

**控制端不做最终放行判断**：真正的执行环境级检查在 Executor（`policy.py`）。
本模块负责：
- 规范化与校验用户声明的范围，拒绝含糊条目；
- 生成不可变 revision，供 `tool_operations.scope_revision` 记录；
- 提供"这条 URL 是否在声明范围内"的**预检**，用于提前给出清楚原因，
  但**不得**因此声称"范围强制保护已生效"（§10）。

严格范围模式保留 scheme/host/port 的精确匹配；按指令模式将用户目标主机写为
显式 any 协议、任意端口规则。两种模式都使用同一份持久化范围与 Executor 检查。
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


class TargetMappingRequired(Exception):
    """用户回环目标的机器归属尚未解析；不是目标连接失败。"""

    def __init__(self, url: str) -> None:
        self.target = url
        super().__init__(
            "尚未建立本机目标映射，未连接目标。localhost / 127.0.0.1 / ::1 在网络工具、"
            "浏览器和命令中指执行岛本机，不能据此判断用户电脑上的服务是否运行。"
            "请提供执行岛可访问的主机地址，或配置显式目标映射；"
            "不要改用 curl、端口扫描、进程或容器检查来重复验证执行岛的回环地址。"
        )


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
    """当前修订的范围策略；用户新指令应用后替换快照。"""

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

        reason = (
            "用户指令中尚未提供这个目标。请在聊天里明确目标网址或 IP，无需填写 scope"
            if self._snapshot.mode == "instruction"
            else "目标不在声明的测试范围内（范围按 scheme/host/port 精确匹配）"
        )
        raise ScopeViolation(reason, target=url)

    def is_allowed(self, url: str) -> bool:
        try:
            self.check_url(url)
        except ScopeViolation:
            return False
        return True

    def loopback_requires_mapping(self, url: str) -> bool:
        """返回回环目标是否缺少匹配的显式映射（含端口与协议）。"""
        target = parse_target(url)
        try:
            loopback = ipaddress.ip_address(target.host).is_loopback
        except ValueError:
            loopback = target.host in _LOOPBACK_HOSTS
        if not loopback:
            return False
        for mapping in self._snapshot.target_mappings:
            if mapping.get("from_host", "").lower().rstrip(".") != target.host:
                continue
            if mapping.get("scheme") and mapping["scheme"] != target.scheme:
                continue
            try:
                if int(mapping.get("from_port", "0")) != target.port:
                    continue
                if not 1 <= int(mapping.get("to_port", "0")) <= 65535:
                    continue
            except (TypeError, ValueError):
                continue
            if mapping.get("to_host", "").strip():
                return False
        return True

    def check_execution_url(self, url: str) -> ParsedTarget:
        """聊天目标不能隐式落到执行岛回环；严格模式保留显式岛内测试。"""
        target = self.check_url(url)
        if self._snapshot.mode == "instruction" and self.loopback_requires_mapping(url):
            raise TargetMappingRequired(url)
        return target


class ScopeDeniedError(Exception):
    """供上层统一映射为 `ErrorCode.SCOPE_DENIED`。"""


def validate_scope(snapshot: ScopeSnapshot) -> None:
    """校验显式规则；空规则表示拒绝全部网络访问，可先进行需求沟通。"""
    for index, rule in enumerate(snapshot.rules):
        if snapshot.mode == "instruction" and (not rule.host or any(c in rule.host for c in "*?[/\\")):
            raise ValueError("按指令模式的目标必须是明确主机或 IP，不能使用通配符")
        if snapshot.mode == "scoped" and rule.scheme == "any" and rule.port is None:
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


def executor_scope_payload(snapshot: ScopeSnapshot) -> dict[str, object]:
    """转换为执行岛 `ScopePayload` 的字段集合。

    执行岛对未知字段 `extra="forbid"`，因此**只发送协议声明过的字段**；
    控制端内部的 `notes` 等展示字段不进载荷。执行岛字段名是 `mappings`，
    与控制端快照的 `target_mappings` 不同，必须显式映射。
    """
    return {
        "revision": snapshot.revision,
        "rules": [
            {
                "scheme": rule.scheme,
                "host": rule.host,
                "port": rule.port,
                "include_subdomains": rule.include_subdomains,
                "allow_private": rule.allow_private,
            }
            for rule in snapshot.rules
        ],
        "mappings": [
            {
                key: value
                for key, value in {
                    "from_host": item.get("from_host"),
                    "from_port": item.get("from_port"),
                    "to_host": item.get("to_host"),
                    "to_port": item.get("to_port"),
                    "host_header": item.get("host_header"),
                    "sni": item.get("sni"),
                    "scheme": item.get("scheme"),
                }.items()
                if value is not None
            }
            for item in snapshot.target_mappings
        ],
        "denied_metadata_hosts": list(snapshot.denied_metadata_hosts),
    }
