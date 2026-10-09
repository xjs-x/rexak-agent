"""按身份隔离的 HTTPX 会话与原始/解析响应分离。

规则（§8.2、§9.2）：
- 复用**按身份隔离**的连接池；原始字节与解析视图分开保存；
- 默认不继承系统代理（避免本地目标意外经过外部代理）；显式配置才启用；
- 逐跳检查重定向范围（由 `NetworkPolicy` 完成）；
- Cookie 只在显式同身份会话转换时同步，**不把 Cookie 字符串全局复制给所有 URL**。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from workbench_executor.policy import NetworkPolicy, PolicyDenied


@dataclass(slots=True)
class RawResponse:
    """一次 HTTP 交互的原始事实。工件落盘由调用方完成。"""

    url: str
    final_url: str
    status_code: int
    reason_phrase: str
    http_version: str
    headers: list[tuple[str, str]]
    body: bytes
    elapsed_ms: float
    redirect_chain: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def content_type(self) -> str:
        for key, value in self.headers:
            if key.lower() == "content-type":
                return value.split(";")[0].strip().lower()
        return "application/octet-stream"

    def decoded_text(self, *, limit: int | None = None) -> tuple[str, str | None]:
        """按**明确声明**的编码解码。

        返回 `(文本, 错误说明)`。不做 GBK/Latin-1 兜底猜测（§16.3）。
        """
        payload = self.body if limit is None else self.body[:limit]
        charset: str | None = None
        for key, value in self.headers:
            if key.lower() == "content-type" and "charset=" in value.lower():
                charset = value.lower().split("charset=", 1)[1].split(";")[0].strip().strip('"')
                break
        candidates = [charset] if charset else ["utf-8"]
        for encoding in candidates:
            if not encoding:
                continue
            try:
                return payload.decode(encoding), None
            except (UnicodeDecodeError, LookupError) as exc:
                return "", f"按 {encoding} 解码失败：{exc}"
        return "", "未声明字符集且不是合法 UTF-8；已保留原始字节"


@dataclass(slots=True)
class IdentitySession:
    """一个测试身份的 HTTP 状态。"""

    identity_id: str
    client: httpx.AsyncClient
    revision: int = 0

    @property
    def cookies(self) -> dict[str, str]:
        """当前身份会话的 Cookie（只读视图）。值缺失的条目被跳过而不是填 None。"""
        return {
            cookie.name: cookie.value
            for cookie in self.client.cookies.jar
            if cookie.value is not None
        }


class HttpSessionRegistry:
    """按身份管理 HTTPX AsyncClient。

    同一身份的 Cookie jar 共享；不同身份之间**不共享任何 Cookie**（ACCEPTANCE A19）。
    """

    def __init__(self, policy: NetworkPolicy, *, use_system_proxy: bool = False) -> None:
        self._policy = policy
        self._sessions: dict[str, IdentitySession] = {}
        self._use_system_proxy = use_system_proxy

    def _new_client(self) -> httpx.AsyncClient:
        # trust_env=False 明确关闭"隐式继承系统代理"，避免本地目标经外部代理（§9.3）
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
            trust_env=self._use_system_proxy,
            verify=True,
        )

    def get(self, identity_id: str) -> IdentitySession:
        session = self._sessions.get(identity_id)
        if session is None:
            session = IdentitySession(identity_id=identity_id, client=self._new_client())
            self._sessions[identity_id] = session
        return session

    async def close(self) -> None:
        for session in self._sessions.values():
            await session.client.aclose()
        self._sessions.clear()

    async def request(
        self,
        *,
        identity_id: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        content: bytes | str | None = None,
        json_body: Any | None = None,
        max_redirects: int = 10,
        max_body_bytes: int = 5 * 1024 * 1024,
    ) -> RawResponse:
        """执行一次请求，**逐跳检查重定向范围**。"""
        session = self.get(identity_id)
        # 显式映射：控制端回环 -> 执行岛可达地址（不做隐式重写）
        mapped_url, extra_headers = self._policy.resolve_mapping(url)
        self._policy.check(mapped_url)

        merged_headers = dict(extra_headers)
        merged_headers.update(headers or {})

        body_bytes: bytes | None
        if json_body is not None:
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/json")
        elif isinstance(content, str):
            body_bytes = content.encode("utf-8")
        else:
            body_bytes = content

        chain: list[str] = [mapped_url]
        current = mapped_url
        current_method = method.upper()
        current_params = params
        current_body = body_bytes

        for _ in range(max_redirects + 1):
            response = await session.client.request(
                current_method,
                current,
                headers=merged_headers,
                params=current_params,
                content=current_body,
                follow_redirects=False,
            )

            location = response.headers.get("location")
            if response.status_code in {301, 302, 303, 307, 308} and location:
                next_url = str(httpx.URL(current).join(location))
                # 逐跳范围检查：拒绝"首次解析通过就当整段任务永久放行"
                self._policy.check(next_url)
                if response.status_code in {301, 302, 303} and current_method not in {
                    "GET",
                    "HEAD",
                }:
                    # 303/302 语义：改为 GET，并丢弃请求体
                    current_method = "GET"
                    current_body = None
                chain.append(next_url)
                current = next_url
                current_params = None
                continue

            body = response.content
            truncated = False
            if len(body) > max_body_bytes:
                body = body[:max_body_bytes]
                truncated = True

            return RawResponse(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                reason_phrase=response.reason_phrase,
                http_version=response.http_version,
                headers=[(k, v) for k, v in response.headers.multi_items()],
                body=body,
                elapsed_ms=response.elapsed.total_seconds() * 1000
                if response.elapsed is not None
                else 0.0,
                redirect_chain=chain,
                truncated=truncated,
            )

        raise PolicyDenied("重定向次数超过上限", target=url)

    # ---------------- 会话转换 ----------------

    def export_cookies(
        self, identity_id: str, *, domain_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """导出 Cookie。只用于**显式同身份**的会话转换；保留域/路径/Secure 属性。"""
        session = self.get(identity_id)
        exported: list[dict[str, Any]] = []
        for cookie in session.client.cookies.jar:
            domain = cookie.domain or ""
            if domain_filter and domain_filter not in domain:
                continue
            exported.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": domain,
                    "path": cookie.path or "/",
                    "secure": bool(cookie.secure),
                    "expires": cookie.expires,
                }
            )
        return exported

    def import_cookies(self, identity_id: str, cookies: list[dict[str, Any]]) -> int:
        """导入 Cookie。调用方必须已完成域/路径/身份版本检查。"""
        session = self.get(identity_id)
        count = 0
        for item in cookies:
            name = str(item.get("name", ""))
            if not name:
                continue
            try:
                session.client.cookies.set(
                    name,
                    str(item.get("value", "")),
                    domain=str(item.get("domain") or ""),
                    path=str(item.get("path") or "/"),
                )
                count += 1
            except (TypeError, ValueError):
                continue
        session.revision += 1
        return count

    def clear(self, identity_id: str) -> None:
        session = self._sessions.pop(identity_id, None)
        if session is not None:
            session.client.cookies.clear()


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    """凭据注入只发生在服务端，不进 prompt / argv / 日志（§9.1）。"""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def same_origin(url_a: str, url_b: str) -> bool:
    a, b = urlsplit(url_a), urlsplit(url_b)
    return (
        a.scheme == b.scheme
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and (a.port or 0) == (b.port or 0)
    )
