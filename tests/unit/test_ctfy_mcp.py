from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_workbench.config.settings import Settings
from agent_workbench.execution.ctfy_mcp import CtfyMcpClient, CtfyMcpError, validate_url
from agent_workbench.tools.ctfy import is_read_only
from agent_workbench.tools.registry import build_tools, default_selection


def config(monkeypatch):
    monkeypatch.setenv("CTFY_TOKEN", "pf_unit_test_only")
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("sse", [False, True])
async def test_real_transport_shapes_auth_and_no_redirects(monkeypatch, sse):
    methods = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer pf_unit_test_only"
        data = json.loads(request.content)
        methods.append(data["method"])
        if data["method"] == "notifications/initialized":
            assert "id" not in data
            assert request.headers["mcp-session-id"] == "test-session"
            return httpx.Response(202)
        if data["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "ctfy"}}
        else:
            result = {"tools": [{"name": "get_competition", "inputSchema": {}}]}
        body = {"jsonrpc": "2.0", "id": data["id"], "result": result}
        headers = {"mcp-session-id": "test-session"}
        if sse:
            return httpx.Response(
                200,
                headers={**headers, "content-type": "text/event-stream"},
                text=": heartbeat\r\ndata: " + json.dumps(body) + "\r\n\r\n",
            )
        return httpx.Response(200, headers=headers, json=body)

    def transport(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        return handler(request)

    async with CtfyMcpClient(
        config(monkeypatch), transport=httpx.MockTransport(transport)
    ) as client:
        assert (await client.list_tools())[0]["name"] == "get_competition"
    assert methods == ["initialize", "notifications/initialized", "tools/list"]


async def test_write_timeout_is_not_retried(monkeypatch):
    calls = []

    def transport(request):
        data = json.loads(request.content)
        calls.append(data["method"])
        if data["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {"protocolVersion": "2025-03-26"},
                },
            )
        if data["method"] == "notifications/initialized":
            return httpx.Response(202)
        raise httpx.ReadTimeout("private provider details must not be shown")

    async with CtfyMcpClient(
        config(monkeypatch), transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(CtfyMcpError, match="超时") as error:
            await client.call_tool("submit_answer", {"instance_id": "test", "answer": "test"})
        assert client.dispatched is True
        assert "private provider" not in str(error.value)
    assert calls.count("tools/call") == 1


async def test_redirect_does_not_forward_bearer(monkeypatch):
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://other.example/mcp/"})

    with pytest.raises(CtfyMcpError, match="HTTP 307"):
        async with CtfyMcpClient(config(monkeypatch), transport=httpx.MockTransport(transport)):
            pytest.fail("must not enter")
    assert calls == ["https://ctfy.tsinghua.edu.cn/api/v1/mcp/"]


def test_server_catalog_controls_tools_and_unknown_effects_are_conservative():
    assert is_read_only("list_competitions", {"name": "list_competitions"})
    assert is_read_only("new_read", {"annotations": {"readOnlyHint": True}})
    assert not is_read_only("contact_organisers", {"name": "contact_organisers"})
    assert not is_read_only("new_tool", {"name": "new_tool"})


@pytest.mark.parametrize(
    "url",
    [
        "http://ctfy.example/api/v1/mcp/",
        "https://user:secret@ctfy.example/api/v1/mcp/",
        "https://ctfy.example/api/v1/mcp/?token=secret",
    ],
)
def test_endpoint_rejects_unprotected_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize("enabled", [False, True])
def test_ctfy_tools_follow_configuration_in_ultra_manifest(enabled):
    from dataclasses import replace

    settings = Settings(_env_file=None, ctfy_mcp_enabled=enabled)  # pyright: ignore[reportCallIssue]
    selection = replace(default_selection(settings), ultra=True)
    names = {item.name for item in build_tools(selection)}
    assert ({"ctfy_list_tools", "ctfy_call_tool"} <= names) is enabled
    assert selection.to_manifest().get("ctfy_mcp", False) is enabled


@pytest.mark.parametrize("body", ["[]", '{"token": 123}', '{"token":', "\ufffd"])
def test_bad_credentials_produce_safe_error(monkeypatch, tmp_path, body):
    from agent_workbench.execution.ctfy_mcp import load_token

    monkeypatch.delenv("CTFY_TOKEN", raising=False)
    path = tmp_path / "credentials.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CtfyMcpError) as error:
        load_token(Settings(_env_file=None, ctfy_mcp_credentials_file=path))  # pyright: ignore[reportCallIssue]
    assert str(path) not in str(error.value)
    assert body not in str(error.value)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


async def test_sse_utf8_and_crlf_can_cross_chunks(monkeypatch):
    client = CtfyMcpClient(config(monkeypatch))
    try:
        response = httpx.Response(
            200, stream=ChunkStream([b'data: "\xe4', b'\xb8\xad"\r', b'\n\r', b'\n'])
        )
        assert [line async for line in client._sse_lines(response)] == ['data: "中"', ""]
    finally:
        await client._client.aclose()


@pytest.mark.parametrize("chunks", [[b"data: \xff\n\n"], [b"data: \xe4"]])
async def test_sse_rejects_invalid_utf8(monkeypatch, chunks):
    client = CtfyMcpClient(config(monkeypatch))
    try:
        with pytest.raises(CtfyMcpError, match="UTF-8"):
            _ = [line async for line in client._sse_lines(httpx.Response(200, stream=ChunkStream(chunks)))]
    finally:
        await client._client.aclose()


async def test_sse_limits_bytes_before_waiting_for_newline(monkeypatch):
    monkeypatch.setattr("agent_workbench.execution.ctfy_mcp.MAX_RESPONSE_BYTES", 8)
    client = CtfyMcpClient(config(monkeypatch))
    try:
        with pytest.raises(CtfyMcpError, match="超过"):
            _ = [line async for line in client._sse_lines(httpx.Response(200, stream=ChunkStream([b"123456789"])))]
    finally:
        await client._client.aclose()


async def test_total_timeout_and_cancellation_are_distinct(monkeypatch):
    async def transport(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("不可达")

    client = CtfyMcpClient(config(monkeypatch), transport=httpx.MockTransport(transport))
    client.timeout = 0.01
    try:
        with pytest.raises(CtfyMcpError, match="总请求超时"):
            await client.call_tool("start_instance", {})
        assert client.dispatched
        client.timeout = 10
        task = asyncio.create_task(client.call_tool("start_instance", {}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client._client.aclose()


async def test_untrusted_rpc_error_code_is_not_echoed(monkeypatch):
    client = CtfyMcpClient(config(monkeypatch))
    try:
        with pytest.raises(CtfyMcpError) as error:
            client._decode(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": client.token}}), 1)
        assert client.token not in str(error.value)
    finally:
        await client._client.aclose()


@pytest.mark.parametrize("name", ["get_competition", "list_competitions", "start_instance", "contact_organisers"])
@pytest.mark.parametrize("replayed", [False, True])
async def test_tool_transport_failure_records_uncertainty_without_retry(monkeypatch, name, replayed):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_workbench.tools import ctfy

    settings = config(monkeypatch).model_copy(update={"ctfy_mcp_enabled": True})
    calls, receipts = [], []
    operation_id = uuid4()

    @asynccontextmanager
    async def lock(keys):
        yield

    @asynccontextmanager
    async def session():
        yield None

    async def current():
        pass

    async def prepare(*args, **kwargs):
        return operation_id, replayed

    async def record(*args, **kwargs):
        receipts.append(kwargs)

    class Repository:
        def __init__(self, db):
            pass

        async def get(self, identifier):
            return SimpleNamespace(result_snapshot=None)

    class Client:
        url = settings.ctfy_mcp_url
        token = "pf_unit_test_only"
        dispatched = False

        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return [{"name": name, "inputSchema": {"properties": {"competition_id": {}}}}]

        async def call_tool(self, tool_name, arguments):
            calls.append(tool_name)
            self.dispatched = True
            raise CtfyMcpError("transport timeout")

    box = SimpleNamespace(
        settings=settings,
        locks=SimpleNamespace(acquire=lock),
        assert_control_current=current,
        db=SimpleNamespace(session=session),
    )
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box), tool_call_id="stable-call")
    monkeypatch.setattr(ctfy, "CtfyMcpClient", Client)
    monkeypatch.setattr(ctfy, "prepare_operation", prepare)
    monkeypatch.setattr(ctfy, "record_result", record)
    monkeypatch.setattr(ctfy, "ToolOperationRepository", Repository)
    call = ctfy.ctfy_call_tool.coroutine  # pyright: ignore[reportAttributeAccessIssue]
    assert call is not None
    output = json.loads(await call(runtime=runtime, name=name, arguments={}))
    if replayed:
        assert output["status"] == "unknown"
        assert calls == [] and receipts == []
        return
    expected = "failed" if name in {"get_competition", "list_competitions"} else "unknown"
    assert output["status"] == expected
    assert len(receipts) == 1 and receipts[0]["status"] == expected
    assert calls == [name]
    assert "transport timeout" not in json.dumps(output)
