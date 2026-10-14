"""结构化请求构造的真实字节验证（**不需要数据库**）。

用一个本地回显服务**独立解析**收到的请求，检查：
普通字段、重复字段名、中文、文本文件 part、CRLF、Content-Type 一致性；
再测 raw 原样保留、手工 multipart 头被拒绝、CSRF 注入、身份隔离与重定向语义。

说明：本文件位于 `integration/` 目录但**不标 integration**，因为它只依赖本地回显服务，
不需要真实 PostgreSQL；这样它可以在任何环境运行，而不是被误标为 NOT_RUN。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlsplit

import pytest
from workbench_executor.http_encoding import BodyConstructionError, FormField, MultipartPart
from workbench_executor.http_sessions import HttpSessionRegistry
from workbench_executor.policy import NetworkPolicy, ScopeRuleSpec
from workbench_executor.settings import ExecutorSettings


class _EchoHandler(BaseHTTPRequestHandler):
    records: ClassVar[list[dict]] = []

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _respond(self, status: int = 200, extra: dict[str, str] | None = None) -> None:
        payload = json.dumps({"ok": True}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, path: str, body: bytes, method: str) -> None:
        type(self).records.append(
            {
                "method": method,
                "path": path,
                "content_type": self.headers.get("Content-Type"),
                "cookie": self.headers.get("Cookie"),
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )

    def _respond_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._record(self.path, b"", "GET")
        if self.path.startswith("/form-with-token"):
            self._respond_html(
                '<html><body><form method="post" action="/submit">'
                '<input type="hidden" name="csrf_token" value="fresh-token">'
                "</form></body></html>"
            )
            return
        self._respond()

    def do_HEAD(self) -> None:
        self._record(self.path, b"", "HEAD")
        self._respond()

    def do_POST(self) -> None:
        body = self._read_body()
        self._record(self.path, body, "POST")
        if self.path.startswith("/redirect303"):
            self._respond(303, {"Location": "/target"})
            return
        if self.path.startswith("/redirect307"):
            self._respond(307, {"Location": "/target"})
            return
        if self.path.startswith("/cookie"):
            self._respond(200, {"Set-Cookie": "sid=abc123; Path=/"})
            return
        self._respond()

    def log_message(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, format: str, *args: Any
    ) -> None:
        return


class _EchoServer:
    def __init__(self) -> None:
        _EchoHandler.records = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def records(self) -> list[dict]:
        return _EchoHandler.records

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def echo():
    server = _EchoServer()
    try:
        yield server
    finally:
        server.close()


def _policy(port: int) -> NetworkPolicy:
    return NetworkPolicy(
        [ScopeRuleSpec(scheme="http", host="127.0.0.1", port=port, allow_private=True)],
        [],
        ExecutorSettings(token="test-token"),
    )


def parse_multipart(body: bytes, content_type: str) -> list[tuple[str, bytes, bytes]]:
    """独立解析 multipart：返回 `[(头部, part 正文, 原始 segment)]`。"""
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    assert match, f"缺少 boundary：{content_type!r}"
    boundary = match.group(1).encode()
    assert body.startswith(b"--" + boundary + b"\r\n"), "首行 boundary 形态不正确"
    assert body.endswith(b"--" + boundary + b"--\r\n"), "缺少结束 boundary"
    parts: list[tuple[str, bytes, bytes]] = []
    for segment in body.split(b"--" + boundary)[1:]:
        if segment in (b"--\r\n", b"--", b""):
            continue
        assert segment.startswith(b"\r\n"), "part 前缺少 CRLF"
        segment = segment[2:]
        head, sep, data = segment.partition(b"\r\n\r\n")
        assert sep == b"\r\n\r\n", "part 头部与正文之间缺少空行"
        assert data.endswith(b"\r\n"), "part 正文后缺少 CRLF"
        parts.append((head.decode("utf-8"), data[:-2], segment))
    return parts


async def test_form_fields_preserve_duplicates_order_and_chinese(echo):
    registry = HttpSessionRegistry(None)
    try:
        response = await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/form"),
            form_fields=[
                {"name": "tag", "value": "一"},
                {"name": "tag", "value": "二"},
                {"name": "note", "value": "中文值 & 符号"},
            ],
            max_redirects=0,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    assert response.status_code == 200
    record = echo.records[-1]
    assert record["content_type"] == "application/x-www-form-urlencoded"
    pairs = parse_qsl(record["body"].decode("utf-8"))
    assert pairs == [("tag", "一"), ("tag", "二"), ("note", "中文值 & 符号")]
    assert response.request_encoding == "form"
    assert response.request_body_bytes == len(record["body"])
    assert len(response.request_body_sha256) == 64


async def test_multipart_crlf_boundary_and_content_type_consistency(echo):
    registry = HttpSessionRegistry(None)
    try:
        response = await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/upload"),
            multipart_parts=[
                {"name": "field", "value": "中文内容"},
                {"name": "file", "value": "line1\nline2", "filename": "a.txt"},
            ],
            max_redirects=0,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    record = echo.records[-1]
    assert record["content_type"].startswith("multipart/form-data; boundary=")
    parts = parse_multipart(record["body"], record["content_type"])
    assert len(parts) == 2
    head_text, data, _ = parts[0]
    assert 'Content-Disposition: form-data; name="field"' in head_text
    assert data.decode("utf-8") == "中文内容"
    file_head, file_data, _ = parts[1]
    assert 'filename="a.txt"' in file_head
    assert "Content-Type: text/plain" in file_head
    assert file_data.decode("utf-8") == "line1\nline2"
    assert response.request_encoding == "multipart"


async def test_raw_body_is_kept_byte_exact_without_auto_content_type(echo):
    registry = HttpSessionRegistry(None)
    raw = "a=1&b=2\r\nX: y\n"
    try:
        await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/raw"),
            content=raw,
            max_redirects=0,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    record = echo.records[-1]
    assert record["body"].decode("utf-8") == raw
    assert record["content_type"] is None


async def test_manual_multipart_content_type_is_refused_before_sending(echo):
    registry = HttpSessionRegistry(None)
    try:
        with pytest.raises(BodyConstructionError):
            await registry.request(
                identity_id="anonymous",
                method="POST",
                url=echo.url("/upload"),
                headers={"Content-Type": "multipart/form-data; boundary=manual"},
                multipart_parts=[{"name": "a", "value": "b"}],
                max_redirects=0,
                policy=_policy(echo.port),
            )
    finally:
        await registry.close()
    assert not [item for item in echo.records if item["path"] == "/upload"]


async def test_csrf_form_injection_replaces_existing_token_and_keeps_one(echo):
    registry = HttpSessionRegistry(None)
    try:
        await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/submit"),
            form_fields=[
                {"name": "csrf_token", "value": "stale"},
                {"name": "user", "value": "admin"},
            ],
            csrf_source_url=echo.url("/form-with-token"),
            csrf_input_name="csrf_token",
            csrf_destination="form",
            max_redirects=0,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    hits = [item for item in echo.records if item["path"] == "/submit"]
    assert len(hits) == 1
    pairs = parse_qsl(hits[0]["body"].decode("utf-8"))
    assert [value for key, value in pairs if key == "csrf_token"] == ["fresh-token"]
    assert ("user", "admin") in pairs


async def test_csrf_form_injection_into_multipart_adds_single_text_part(echo):
    registry = HttpSessionRegistry(None)
    try:
        await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/submit"),
            multipart_parts=[
                {"name": "csrf_token", "value": "stale"},
                {"name": "field", "value": "值"},
            ],
            csrf_source_url=echo.url("/form-with-token"),
            csrf_input_name="csrf_token",
            csrf_destination="form",
            max_redirects=0,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    hits = [item for item in echo.records if item["path"] == "/submit"]
    assert len(hits) == 1
    parts = parse_multipart(hits[0]["body"], hits[0]["content_type"])
    token_parts = [data for head, data, _ in parts if 'name="csrf_token"' in head]
    assert [item.decode("utf-8") for item in token_parts] == ["fresh-token"]
    assert any('name="field"' in head for head, _, _ in parts)


async def test_identity_sessions_do_not_share_cookies(echo):
    registry = HttpSessionRegistry(None)
    try:
        await registry.request(
            identity_id="identity-a",
            method="POST",
            url=echo.url("/cookie"),
            max_redirects=0,
            policy=_policy(echo.port),
        )
        await registry.request(
            identity_id="identity-b",
            method="GET",
            url=echo.url("/cookie"),
            max_redirects=0,
            policy=_policy(echo.port),
        )
        await registry.request(
            identity_id="identity-a",
            method="GET",
            url=echo.url("/cookie"),
            max_redirects=0,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    first, second, third = echo.records[0], echo.records[1], echo.records[2]
    assert first["cookie"] is None
    assert second["cookie"] is None, "不同身份之间不得共享 Cookie"
    assert third["cookie"] == "sid=abc123"


async def test_303_drops_body_and_307_resends_same_encoded_bytes(echo):
    registry = HttpSessionRegistry(None)
    try:
        await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/redirect303"),
            json_body={"a": 1},
            max_redirects=5,
            policy=_policy(echo.port),
        )
        await registry.request(
            identity_id="anonymous",
            method="POST",
            url=echo.url("/redirect307"),
            json_body={"a": 1},
            max_redirects=5,
            policy=_policy(echo.port),
        )
    finally:
        await registry.close()
    target_hits = [item for item in echo.records if item["path"] == "/target"]
    assert len(target_hits) == 2
    assert target_hits[0]["method"] == "GET" and target_hits[0]["body"] == b""
    assert target_hits[0]["content_type"] is None
    assert target_hits[1]["method"] == "POST"
    assert target_hits[1]["body"] == json.dumps({"a": 1}, ensure_ascii=False).encode("utf-8")


async def test_concurrent_requests_keep_their_own_bodies(echo):
    registry = HttpSessionRegistry(None)
    try:
        await asyncio.gather(
            *(
                registry.request(
                    identity_id="anonymous",
                    method="POST",
                    url=echo.url(f"/concurrent-{index}"),
                    form_fields=[{"name": "n", "value": str(index)}],
                    max_redirects=0,
                    policy=_policy(echo.port),
                )
                for index in range(4)
            )
        )
    finally:
        await registry.close()
    seen = {urlsplit(item["path"]).path: parse_qsl(item["body"].decode()) for item in echo.records}
    for index in range(4):
        assert seen[f"/concurrent-{index}"] == [("n", str(index))]


def test_form_and_multipart_models_reject_blank_names() -> None:
    with pytest.raises(ValueError):
        FormField(name="", value="x")
    with pytest.raises(ValueError):
        MultipartPart(name="", value="x")
