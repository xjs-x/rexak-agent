"""执行岛网络与范围策略。

**这里才是最终放行判断**（§10）。控制端的预检只是提前给出原因。

能力边界（必须诚实标注）：
- HTTP 与浏览器请求可以逐跳检查目标；
- **自编脚本能绕开 Python 工具的 URL 校验**。因此无人值守的通用命令模式必须具备
  **执行环境级的出站网络约束**；正则黑名单、工作目录或提示词都**不能**宣称是网络隔离。
- 环境无法提供该约束时，能力预检必须明确关闭通用命令模式，或把它标记为
  "仅用于专门隔离测试网络"，**不得**显示"范围强制保护已生效"。
"""

from __future__ import annotations

import fnmatch
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from workbench_executor.settings import ExecutorSettings

DEFAULT_PORTS = {"http": 80, "https": 443}

METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)


class PolicyDenied(Exception):
    def __init__(self, reason: str, *, target: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.target = target


@dataclass(frozen=True, slots=True)
class ScopeRuleSpec:
    """由控制端随任务下发的范围规则。"""

    scheme: str = "any"
    host: str = "*"
    port: int | None = None
    include_subdomains: bool = False
    allow_private: bool = True


@dataclass(frozen=True, slots=True)
class TargetMapping:
    """控制端 localhost -> 执行岛可达地址的显式映射（§9.3）。

    `host_header` / `sni` 用于保留虚拟主机与 TLS 语义，避免"偷偷重写 URL"。
    """

    from_host: str
    from_port: int
    to_host: str
    to_port: int
    host_header: str | None = None
    sni: str | None = None
    scheme: str | None = None


class NetworkPolicy:
    """一次任务范围内的实际连接检查。"""

    def __init__(
        self,
        rules: list[ScopeRuleSpec],
        mappings: list[TargetMapping],
        settings: ExecutorSettings,
        *,
        denied_hosts: set[str] | None = None,
    ) -> None:
        self._rules = tuple(rules)
        self._mappings = tuple(mappings)
        self._settings = settings
        self._denied = set(denied_hosts or ()) | set(METADATA_HOSTS)
        # 执行岛自身的服务端口属于管理面，不得被测
        self._own_ports = {settings.port}

    @property
    def network_isolation(self) -> str:
        return self._settings.network_isolation

    @property
    def generic_command_allowed(self) -> bool:
        """通用命令模式只有在具备执行环境级出站约束时才允许无人值守使用。

        首版无该系统级约束，因此默认**关闭**通用命令的无人值守模式，
        并在 `/capabilities` 中如实暴露 `network_isolation=unavailable`。
        """
        return self._settings.network_isolation == "enforced"

    def resolve_mapping(self, url: str) -> tuple[str, dict[str, str]]:
        """把控制端 URL 映射为执行岛实际访问地址。

        返回 `(实际 URL, 需要附加的头部)`。**不做隐式重写**：没有匹配映射时原样返回。
        """
        parts = urlsplit(url if "://" in url else f"http://{url}")
        host = (parts.hostname or "").lower()
        scheme = (parts.scheme or "http").lower()
        port = parts.port or DEFAULT_PORTS.get(scheme, 80)

        for mapping in self._mappings:
            if mapping.from_host.lower() != host:
                continue
            if mapping.from_port != port:
                continue
            if mapping.scheme and mapping.scheme != scheme:
                continue
            netloc = f"{mapping.to_host}:{mapping.to_port}"
            rebased = f"{scheme}://{netloc}{parts.path or '/'}"
            if parts.query:
                rebased += f"?{parts.query}"
            headers: dict[str, str] = {}
            if mapping.host_header:
                headers["Host"] = mapping.host_header
            return rebased, headers

        return url, {}

    def check(self, url: str) -> str:
        """检查一个实际要连接的目标（含重定向的每一跳）。返回被允许的 URL。"""
        parts = urlsplit(url if "://" in url else f"http://{url}")
        host = (parts.hostname or "").lower().rstrip(".")
        scheme = (parts.scheme or "http").lower()
        port = parts.port or DEFAULT_PORTS.get(scheme, 80)

        if not host:
            raise PolicyDenied("目标缺少主机名", target=url)
        if host in self._denied:
            raise PolicyDenied("目标是云元数据地址，属于非目标管理面", target=url)

        # 执行岛管理端口：避免测试到 Executor 自身管理接口
        if host in {"127.0.0.1", "localhost", "::1"} and port in self._own_ports:
            raise PolicyDenied("目标是执行岛自身的 Executor 管理端口，属于非目标管理面", target=url)

        for rule in self._rules:
            if rule.scheme not in {"any", scheme}:
                continue
            if rule.port is not None and rule.port != port:
                continue
            if not self._host_matches(rule.host, host, rule.include_subdomains):
                continue
            if self._is_private(host) and not rule.allow_private:
                raise PolicyDenied("目标是私网地址且规则未允许私网", target=url)
            return url

        raise PolicyDenied("目标不在声明的测试范围内", target=url)

    def check_redirect_chain(self, urls: list[str]) -> list[str]:
        """逐跳检查。任一跳越界即拒绝。"""
        return [self.check(item) for item in urls]

    @staticmethod
    def _host_matches(pattern: str, host: str, include_subdomains: bool) -> bool:
        if pattern in {"*", ""}:
            return True
        lowered = pattern.lower().rstrip(".")
        if any(token in lowered for token in "*?["):
            return fnmatch.fnmatchcase(host, lowered)
        if lowered == host:
            return True
        if include_subdomains:
            return host.endswith("." + lowered)
        return False

    @staticmethod
    def _is_private(host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return address.is_private or address.is_loopback or address.is_link_local


def command_patterns_forbidden() -> tuple[str, ...]:
    """**仅作辅助告警**，不构成隔离。

    明确在文档与 `/capabilities` 中说明：正则黑名单不能防止脚本绕过范围检查。
    """
    return (
        "*curl*",
        "*wget*",
        "*nc *",
        "*ncat*",
        "*socat*",
        "*ssh *",
        "*scp *",
        "*rsync *",
        "*nmap*",
        "*masscan*",
        "*docker*",
        "*kubectl*",
    )
