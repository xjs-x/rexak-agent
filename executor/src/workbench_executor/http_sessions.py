"""按身份隔离的 HTTPX 会话与原始/解析响应分离。

规则（§8.2、§9.2）：
- 复用**按身份隔离**的连接池；原始字节与解析视图分开保存；
- 默认不继承系统代理（避免本地目标意外经过外部代理）；显式配置才启用；
- 逐跳检查重定向范围（由 `NetworkPolicy` 完成）；
- Cookie 只在显式同身份会话转换时同步，**不把 Cookie 字符串全局复制给所有 URL**。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from workbench_executor.policy import NetworkPolicy, PolicyDenied


class CsrfTokenUnavailable(ValueError):
    """当前页面没有可用的短时表单 token；本次写请求未发出。"""


class _HiddenInputParser(HTMLParser):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        fields = dict(attrs)
        if fields.get("name") == self.name and fields.get("type") == "hidden":
            self.value = fields.get("value")


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

    def __init__(self, policy: NetworkPolicy | None, *, use_system_proxy: bool = False) -> None:
        self._policy = policy
        self._sessions: dict[str, IdentitySession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._use_system_proxy = use_system_proxy

    def _new_client(self, *, verify_tls: bool = True) -> httpx.AsyncClient:
        # trust_env=False 明确关闭"隐式继承系统代理"，避免本地目标经外部代理（§9.3）
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
            trust_env=self._use_system_proxy,
            verify=verify_tls,
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
        self._locks.clear()

    @contextlib.asynccontextmanager
    async def _request_client(
        self, identity_id: str, verify_tls: bool
    ) -> AsyncIterator[httpx.AsyncClient]:
        session = self.get(identity_id)
        if verify_tls:
            yield session.client
            return
        # 不修改身份的默认连接池；临时连接池只共享该身份 Cookie jar，并在请求后关闭。
        async with self._new_client(verify_tls=False) as client:
            client.cookies = session.client.cookies.jar
            yield client

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
        policy: NetworkPolicy | None = None,
        csrf_source_url: str | None = None,
        csrf_input_name: str | None = None,
        csrf_destination: str = "form",
        csrf_target_name: str | None = None,
        verify_tls: bool = True,
    ) -> RawResponse:
        """执行一次请求，**逐跳检查重定向范围**。"""
        active_policy = policy or self._policy
        if active_policy is None:
            raise PolicyDenied("HTTP 请求缺少范围策略", target=url)
        if max_body_bytes < 0:
            raise ValueError("响应体上限不能为负数")
        # CSRF 刷新前先核对最终写目标，避免越界写请求附带一次不必要的来源 GET。
        active_policy.check(active_policy.resolve_mapping(url)[0])
        if csrf_source_url:
            active_policy.check(active_policy.resolve_mapping(csrf_source_url)[0])
            if not csrf_input_name or method.upper() in {"GET", "HEAD"}:
                raise CsrfTokenUnavailable("CSRF 刷新需要输入名和写请求方法")
            if csrf_destination == "form" and json_body is not None:
                raise CsrfTokenUnavailable("form 注入不能与 JSON 请求体同用")
            if csrf_destination == "json" and not isinstance(json_body, dict):
                raise CsrfTokenUnavailable("json 注入需要 JSON 对象请求体")
            if csrf_destination not in {"form", "json", "header"}:
                raise CsrfTokenUnavailable("不支持的 CSRF 注入位置")
        lock = self._locks.setdefault(identity_id, asyncio.Lock())
        async with lock, self._request_client(identity_id, verify_tls) as client:
            csrf_sensitive_header = ""
            if csrf_source_url:
                assert csrf_input_name is not None
                source = await self._request_locked(
                    identity_id=identity_id,
                    method="GET",
                    url=csrf_source_url,
                    headers=None,
                    params=None,
                    content=None,
                    json_body=None,
                    max_redirects=max_redirects,
                    max_body_bytes=1_048_576,
                    policy=active_policy,
                    client=client,
                )
                if source.status_code >= 400 or source.truncated:
                    raise CsrfTokenUnavailable("CSRF 来源页面不可用或超过读取上限")
                source_text, decode_error = source.decoded_text()
                if decode_error:
                    raise CsrfTokenUnavailable("CSRF 来源页面无法按声明编码读取")
                parser = _HiddenInputParser(csrf_input_name)
                parser.feed(source_text)
                if not parser.value:
                    raise CsrfTokenUnavailable("CSRF 来源页面没有指定的隐藏输入项")
                target_name = csrf_target_name or csrf_input_name
                headers = dict(headers or {})
                if csrf_destination == "form":
                    pairs = [
                        (key, value)
                        for key, value in parse_qsl(str(content or ""))
                        if key != target_name
                    ]
                    content = urlencode([*pairs, (target_name, parser.value)])
                    if not any(key.lower() == "content-type" for key in headers):
                        headers["Content-Type"] = "application/x-www-form-urlencoded"
                elif csrf_destination == "json":
                    assert isinstance(json_body, dict)
                    json_body = {**json_body, target_name: parser.value}
                elif csrf_destination == "header":
                    headers[target_name] = parser.value
                    csrf_sensitive_header = target_name.lower()
            return await self._request_locked(
                identity_id=identity_id,
                method=method,
                url=url,
                headers=headers,
                params=params,
                content=content,
                json_body=json_body,
                max_redirects=max_redirects,
                max_body_bytes=max_body_bytes,
                policy=active_policy,
                sensitive_header_name=csrf_sensitive_header,
                forbid_cross_origin_redirect=bool(csrf_source_url),
                client=client,
            )

    async def _request_locked(
        self,
        *,
        identity_id: str,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
        content: bytes | str | None,
        json_body: Any | None,
        max_redirects: int,
        max_body_bytes: int,
        policy: NetworkPolicy,
        sensitive_header_name: str = "",
        forbid_cross_origin_redirect: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> RawResponse:
        session = self.get(identity_id)
        request_headers = dict(headers or {})

        body_bytes: bytes | None
        if json_body is not None:
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif isinstance(content, str):
            body_bytes = content.encode("utf-8")
        else:
            body_bytes = content

        chain: list[str] = []
        current_logical = url
        current_method = method.upper()
        current_params = params
        current_body = body_bytes

        for _ in range(max_redirects + 1):
            # 每一跳重新映射并复核；跨 origin 不转发显式凭据和原始 Host。
            current, mapping_headers = policy.resolve_mapping(current_logical)
            policy.check(current)
            hop_headers = dict(request_headers)
            if not same_origin(url, current_logical):
                hop_headers = {
                    key: value
                    for key, value in hop_headers.items()
                    if key.lower()
                    not in {
                        "authorization",
                        "cookie",
                        "proxy-authorization",
                        "host",
                        sensitive_header_name,
                    }
                }
            hop_headers.update(mapping_headers)
            chain.append(current)
            request_started = time.perf_counter()
            async with (client or session.client).stream(
                current_method,
                current,
                headers=hop_headers,
                params=current_params,
                content=current_body,
                follow_redirects=False,
            ) as response:
                location = response.headers.get("location")
                if response.status_code in {301, 302, 303, 307, 308} and location:
                    next_url = str(httpx.URL(current_logical).join(location))
                    # 逐跳范围检查：拒绝"首次解析通过就当整段任务永久放行"
                    policy.check(policy.resolve_mapping(next_url)[0])
                    if forbid_cross_origin_redirect and not same_origin(url, next_url):
                        raise PolicyDenied(
                            "含 CSRF token 的请求不得跨 origin 重定向", target=next_url
                        )
                    if response.status_code in {301, 302, 303} and current_method not in {
                        "GET",
                        "HEAD",
                    }:
                        current_method = "GET"
                        current_body = None
                    current_logical = next_url
                    current_params = None
                    continue

                body = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    remaining = max_body_bytes - len(body)
                    if len(chunk) > remaining:
                        body.extend(chunk[:remaining])
                        truncated = True
                        break
                    body.extend(chunk)

                return RawResponse(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    reason_phrase=response.reason_phrase,
                    http_version=response.http_version,
                    headers=[(k, v) for k, v in response.headers.multi_items()],
                    body=bytes(body),
                    elapsed_ms=(time.perf_counter() - request_started) * 1000,
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

    async def import_cookies(self, identity_id: str, cookies: list[dict[str, Any]]) -> int:
        """导入 Cookie。调用方必须已完成域/路径/身份版本检查。"""
        lock = self._locks.setdefault(identity_id, asyncio.Lock())
        async with lock:
            return self._import_cookies_locked(identity_id, cookies)

    def _import_cookies_locked(self, identity_id: str, cookies: list[dict[str, Any]]) -> int:
        session = self.get(identity_id)
        count = 0
        for item in cookies:
            name = str(item.get("name", ""))
            domain = str(item.get("domain") or "").lower()
            if not name or not domain:
                continue
            try:
                expires_raw = item.get("expires")
                expires = int(expires_raw) if expires_raw not in (None, -1) else None
                path = str(item.get("path") or "/")
                session.client.cookies.jar.set_cookie(
                    Cookie(
                        version=0,
                        name=name,
                        value=str(item.get("value", "")),
                        port=None,
                        port_specified=False,
                        domain=domain,
                        domain_specified=domain.startswith("."),
                        domain_initial_dot=domain.startswith("."),
                        path=path,
                        path_specified=True,
                        secure=bool(item.get("secure")),
                        expires=expires,
                        discard=expires is None,
                        comment=None,
                        comment_url=None,
                        rest={"HttpOnly": "true"} if item.get("httpOnly") else {},
                        rfc2109=False,
                    )
                )
                count += 1
            except (TypeError, ValueError):
                continue
        session.revision += 1
        return count

    async def clear(self, identity_id: str) -> None:
        lock = self._locks.setdefault(identity_id, asyncio.Lock())
        async with lock:
            session = self._sessions.pop(identity_id, None)
            if session is not None:
                await session.client.aclose()


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    """凭据注入只发生在服务端，不进 prompt / argv / 日志（§9.1）。"""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def same_origin(url_a: str, url_b: str) -> bool:
    a, b = urlsplit(url_a), urlsplit(url_b)
    return (
        a.scheme == b.scheme
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and (a.port or (443 if a.scheme == "https" else 80))
        == (b.port or (443 if b.scheme == "https" else 80))
    )
