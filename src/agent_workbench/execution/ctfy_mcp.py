"""CTFy Streamable HTTP 客户端。只连接运维指定地址，不执行服务端代码。

当前实现覆盖 initialize、notifications/initialized、tools/list、tools/call，
以及 JSON/SSE 响应；不声明 sampling、roots 或 elicitation 能力。
写请求不做自动重试：MCP request id 本身不是服务端幂等保证。
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from agent_workbench.config.settings import Settings

PROTOCOL_VERSION = "2025-03-26"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
log = structlog.get_logger(__name__)


class CtfyMcpError(RuntimeError):
    """不携带令牌、响应正文或认证头的可展示错误。"""


def load_token(settings: Settings) -> str:
    value = os.environ.get("CTFY_TOKEN", "").strip()
    if not value:
        try:
            payload = json.loads(settings.ctfy_mcp_credentials_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CtfyMcpError("CTFy 凭据文件不可读或不是有效 UTF-8 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
            raise CtfyMcpError("CTFy 凭据文件必须包含字符串 token 字段")
        value = payload["token"].strip()
    if not value.startswith("pf_") or any(c.isspace() for c in value):
        raise CtfyMcpError("CTFy 令牌缺失或格式无效；配置 CTFY_TOKEN 或独立凭据文件")
    return value


def validate_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/api/v1/mcp/"
    ):
        raise ValueError("CTFy MCP 地址必须是无凭据、无查询参数的 HTTPS /api/v1/mcp/")
    return value


class CtfyMcpClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = validate_url(settings.ctfy_mcp_url)
        self.token = load_token(settings)
        self.timeout = settings.ctfy_mcp_timeout_seconds
        self.dispatched = False
        self._next_id = 0
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json, text/event-stream",
            },
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> CtfyMcpClient:
        try:
            response = await self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "rexak-agent", "version": "0.1.0"},
                },
            )
            if response.get("protocolVersion") != PROTOCOL_VERSION:
                raise CtfyMcpError("CTFy MCP 返回了当前客户端未支持的协议版本")
            self._client.headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            await self._rpc("notifications/initialized", {}, notification=True)
            return self
        except BaseException:
            await self._client.aclose()
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        # 不在关闭时重新提交操作。会话如由服务器创建，仅做有界的删除。
        try:
            if self._client.headers.get("Mcp-Session-Id"):
                try:
                    async with asyncio.timeout(5):
                        async with self._client.stream("DELETE", self.url, timeout=5):
                            pass
                except (httpx.HTTPError, TimeoutError) as cleanup_error:
                    log.warning(
                        "ctfy_mcp_session_cleanup_failed", error_type=type(cleanup_error).__name__
                    )
        finally:
            await self._client.aclose()

    def _decode(self, raw: str, request_id: int) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise CtfyMcpError("CTFy MCP 返回了无效 JSON") from exc
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise CtfyMcpError("CTFy MCP 返回了无效 JSON-RPC 信封")
        if payload.get("id") != request_id:
            return None
        if "error" in payload:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            code = code if type(code) is int else None
            raise CtfyMcpError(f"CTFy MCP JSON-RPC 错误（code={code}）")
        if not isinstance(payload.get("result"), dict):
            raise CtfyMcpError("CTFy MCP 响应缺少 result 对象")
        return payload["result"]

    async def _rpc(
        self, method: str, params: dict[str, Any], *, notification: bool = False
    ) -> dict[str, Any]:
        # HTTP read timeout 只限制相邻数据包，SSE 心跳不能无限延长本次调用。
        try:
            async with asyncio.timeout(self.timeout):
                return await self._request(method, params, notification=notification)
        except TimeoutError as exc:
            raise CtfyMcpError("CTFy MCP 总请求超时；操作结果需核对，不自动重放") from exc

    async def _request(
        self, method: str, params: dict[str, Any], *, notification: bool = False
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = request_id
        try:
            async with self._client.stream("POST", self.url, json=body) as response:
                if not 200 <= response.status_code < 300:
                    raise CtfyMcpError(f"CTFy MCP HTTP {response.status_code}；未自动重试")
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self._client.headers["Mcp-Session-Id"] = session_id
                if notification:
                    return {}
                media = response.headers.get("content-type", "").split(";", 1)[0].strip()
                if media == "application/json":
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise CtfyMcpError("CTFy MCP 响应超过 4 MiB；请缩小查询页")
                        chunks.append(chunk)
                    try:
                        text = b"".join(chunks).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise CtfyMcpError("CTFy MCP 响应不是 UTF-8") from exc
                    result = self._decode(text, request_id)
                    if result is not None:
                        return result
                elif media == "text/event-stream":
                    data: list[str] = []
                    async for line in self._sse_lines(response):
                        if line == "":
                            if data:
                                result = self._decode("\n".join(data), request_id)
                                if result is not None:
                                    return result
                                data = []
                        elif line.startswith("data:"):
                            data.append(line[5:].removeprefix(" "))
                    if data:
                        result = self._decode("\n".join(data), request_id)
                        if result is not None:
                            return result
                else:
                    raise CtfyMcpError("CTFy MCP 返回了不支持的 Content-Type")
        except httpx.TimeoutException as exc:
            raise CtfyMcpError("CTFy MCP 请求超时；操作结果需核对，不自动重放") from exc
        except httpx.HTTPError as exc:
            raise CtfyMcpError(f"CTFy MCP 连接错误（{type(exc).__name__}）；未自动重试") from exc
        raise CtfyMcpError("CTFy MCP 响应中没有匹配的 request id")

    async def _sse_lines(self, response: httpx.Response) -> AsyncIterator[str]:
        # 在行缓冲前限制实际字节数；HTTPX 的文本解码默认会替换非法 UTF-8。
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending, size = "", 0
        try:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise CtfyMcpError("CTFy MCP SSE 响应超过 4 MiB")
                pending += decoder.decode(chunk)
                while match := re.search(r"\r\n|\r|\n", pending):
                    # CRLF 可以跨 HTTP chunk，保留末尾 CR 等待下一块。
                    if match.group() == "\r" and match.end() == len(pending):
                        break
                    yield pending[: match.start()]
                    pending = pending[match.end() :]
            pending += decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise CtfyMcpError("CTFy MCP SSE 响应不是 UTF-8") from exc
        if pending:
            for line in re.split(r"\r\n|\r|\n", pending):
                yield line

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursors: set[str] = set()
        cursor = None
        for _ in range(20):
            page = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            items = page.get("tools")
            if not isinstance(items, list) or any(not isinstance(t, dict) for t in items):
                raise CtfyMcpError("CTFy MCP 工具目录格式无效")
            tools.extend(items)
            cursor = page.get("nextCursor")
            if not cursor:
                names = [tool.get("name") for tool in tools]
                if any(not isinstance(n, str) for n in names) or len(names) != len(set(names)):
                    raise CtfyMcpError("CTFy MCP 工具名称缺失或重复")
                return tools
            if not isinstance(cursor, str) or cursor in cursors:
                raise CtfyMcpError("CTFy MCP 工具目录游标无效或重复")
            cursors.add(cursor)
        raise CtfyMcpError("CTFy MCP 工具目录超过分页上限")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.dispatched = True
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})


def structured_result(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("structuredContent")
    if isinstance(value, dict):
        return value
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                value = json.loads(block.get("text", ""))
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise CtfyMcpError("CTFy MCP 响应缺少可核对的结构化结果")
