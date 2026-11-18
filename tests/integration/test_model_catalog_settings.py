"""Isolated model settings acceptance against a real loopback HTTP fixture."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from agent_workbench.api.app import create_app
from agent_workbench.config import model_settings
from agent_workbench.config.model_settings import read_model_key, read_preferences, write_model_key
from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.config.worker_model import shared_worker_model
from agent_workbench.models.provider import (
    ModelBinding,
    build_chat_model,
    request_payload,
    resolve_provider_profile,
)

pytestmark = pytest.mark.integration
API = "/api/settings/models"
KEY = "fixture-catalog-key"
TEMP_KEY = "fixture-draft-key"
SNAPSHOT = "deepseek-v4.1-flash-1017"


@pytest.fixture
def directory_server():
    requests: list[tuple[str, str | None, dict[str, list[str]]]] = []

    class DirectoryHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            requests.append((url.path, self.headers.get("Authorization"), query))
            status, body, headers = (
                200,
                {"data": [{"id": SNAPSHOT}, {"id": "org/chat-model"}, {"id": SNAPSHOT}]},
                {},
            )
            if url.path.startswith("/status/"):
                status, body = int(url.path.split("/")[2]), {"echo": KEY}
            elif url.path.startswith("/slow/"):
                time.sleep(2)
            elif url.path == "/redirect/models":
                status, body = 302, {}
                port = self.headers["Host"].partition(":")[2]
                headers["Location"] = f"http://localhost:{port}/capture"
            elif url.path == "/misformed/models":
                body = {"unexpected": []}
            elif url.path == "/bad-row/models":
                body = {"data": [{"id": None}]}
            elif url.path == "/plaintext/models":
                body = b"not JSON"
            elif url.path == "/empty/models":
                body = {"data": []}
            elif url.path == "/echo/models":
                body = {"data": [{"id": KEY}]}
            elif url.path == "/huge/models":
                body = b"x" * (4 * 1024 * 1024 + 1)
            elif url.path == "/official/catalog":
                page = int(query["page_no"][0])
                assert query["page_size"] == ["50"]
                rows = (
                    [
                        {
                            "model": SNAPSHOT,
                            "model_info": {"context_window": 8192, "max_output_tokens": 512},
                        }
                    ]
                    if page == 1
                    else [{"model": SNAPSHOT}, {"model": "official-second"}]
                )
                body = {
                    "success": True,
                    "output": {"models": rows, "page_no": page, "page_size": 1, "total": 2},
                }
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, format: str, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), DirectoryHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def settings_client(database):
    with TestClient(create_app()) as client:
        csrf = client.post("/api/session").json()["csrf_token"]
        yield client, {"X-Workbench-CSRF": csrf}


def provider_payload(base: str, **updates):
    return {
        "revision": 0,
        "provider_id": "office-gateway",
        "display_name": "办公 模型网关",
        "base_url": base,
        "api_key": KEY,
        "models": [
            {
                "model_id": SNAPSHOT,
                "max_output_tokens": 4096,
                "context_window": 32000,
                "request_output_limit": 256,
                "supports_tools": "supported",
                "specification_source": "user",
            }
        ],
        "default_model": SNAPSHOT,
        **updates,
    }


def test_migration_edit_delete_and_historical_worker_binding(settings_client, directory_server):
    client, headers = settings_client
    base, calls = directory_server
    cfg = get_settings()
    path = model_settings.preferences_path(cfg)
    legacy = {
        "default_provider": "bailian",
        "default_model": "deepseek-v4.1-flash",
        "endpoints": {"bailian": "https://legacy.example.test/compatible-mode/v1"},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    write_model_key("bailian", "fixture-existing-key", cfg)
    initial = client.get(API).json()
    assert initial["default_model"] == legacy["default_model"]
    assert any(
        p["provider_id"] == "bailian" and p["secret_configured"] for p in initial["providers"]
    )
    assert json.loads(path.read_text(encoding="utf-8")) == legacy

    response = client.post(
        API + "/providers", json=provider_payload(base + "/v1/chat/completions/"), headers=headers
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["default_provider"] == "office-gateway"
    assert updated["default_model"] == SNAPSHOT
    assert json.loads(path.with_name("models.v1.backup.json").read_text(encoding="utf-8")) == legacy
    assert KEY not in response.text and KEY not in path.read_text(encoding="utf-8")
    if sys.platform == "win32":
        assert KEY not in model_settings.model_secret_path(cfg).read_text(encoding="utf-8")
    assert read_model_key("bailian", cfg) == "fixture-existing-key"
    profile = resolve_provider_profile(settings=cfg)
    assert profile.base_url == base + "/v1"
    assert (profile.max_output_tokens, profile.effective_output_limit, profile.supports_tools) == (
        4096,
        256,
        "supported",
    )
    snapshot = ModelBinding(profile=profile).to_snapshot()
    main = build_chat_model(ModelBinding.from_snapshot(snapshot))
    wire = request_payload(main, [HumanMessage(content="ping")])
    assert wire["model"] == SNAPSHOT and wire["max_completion_tokens"] == 256
    assert KEY not in repr(wire)
    assert shared_worker_model(cfg, snapshot).api_key.get_secret_value() == KEY
    assert shared_worker_model(cfg, snapshot).adapter == "generic"

    known = {"saved_provider_id": "office-gateway", "base_url": base + "/edited"}
    discovered = client.post(API + "/discover", json=known, headers=headers)
    assert discovered.status_code == 200, discovered.text
    assert calls[-1] == ("/edited/models", f"Bearer {KEY}", {})

    updated_id = updated["revision"]
    duplicate = client.post(
        API + "/providers",
        json=provider_payload(base, revision=updated_id, provider_id="OFFICE-gateway"),
        headers=headers,
    )
    assert duplicate.status_code == 422 and duplicate.json()["details"]["field"] == "provider_id"
    second = client.post(
        API + "/providers",
        json=provider_payload(
            base,
            revision=updated_id,
            provider_id="second-service",
            api_key=TEMP_KEY,
            default_model=None,
        ),
        headers=headers,
    ).json()
    assert sum(p["model_name"] == SNAPSHOT for p in second["profiles"]) == 2
    denied = client.put(
        API + "/providers/office-gateway",
        json=provider_payload(
            base.replace("127.0.0.1", "localhost"),
            revision=second["revision"],
            api_key=None,
        ),
        headers=headers,
    )
    assert denied.status_code == 422 and denied.json()["details"]["field"] == "api_key"
    edited = client.put(
        API + "/providers/office-gateway",
        json=provider_payload(
            base.replace("127.0.0.1", "localhost"),
            revision=second["revision"],
            api_key=TEMP_KEY,
            display_name="办公室 新名称",
            models=[{"model_id": "custom-release/unknown"}],
            default_model="custom-release/unknown",
        ),
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    stored = (read_preferences(cfg).providers or {})["office-gateway"]
    assert stored.display_name == "办公室 新名称"
    assert stored.models[0].supports_tools == "unknown"
    assert stored.models[0].context_window is None and stored.models[0].max_output_tokens is None
    assert read_model_key(stored.credential_slot, cfg) == TEMP_KEY
    assert shared_worker_model(cfg, snapshot).api_key.get_secret_value() == KEY
    revision = edited.json()["revision"]
    assert (
        client.delete(
            API + "/providers/office-gateway", params={"revision": revision - 1}, headers=headers
        ).status_code
        == 409
    )
    deleted = client.delete(
        API + "/providers/office-gateway", params={"revision": revision}, headers=headers
    )
    assert deleted.status_code == 200 and deleted.json()["default_provider"] != "office-gateway"
    assert shared_worker_model(cfg, snapshot).api_key.get_secret_value() == KEY


def test_draft_discovery_statuses_shape_limits_and_pagination(
    settings_client, directory_server, monkeypatch
):
    client, headers = settings_client
    base, calls = directory_server
    path = model_settings.preferences_path(get_settings())

    def discover(**updates):
        return client.post(
            API + "/discover",
            json={"base_url": base + "/prefix/v1/chat/completions/", "api_key": KEY, **updates},
            headers=headers,
        )

    assert client.post(API + "/discover", json={}).status_code == 422
    assert client.post(API + "/discover", json={"base_url": base}).status_code == 403
    response = discover(api_key=TEMP_KEY)
    assert response.status_code == 200, response.text
    assert [p["model_id"] for p in response.json()["models"]] == [SNAPSHOT, "org/chat-model"]
    assert all(p["specification_source"] == "unknown" for p in response.json()["models"])
    assert calls[-1] == ("/prefix/v1/models", f"Bearer {TEMP_KEY}", {})
    assert not path.exists() and not model_settings.model_secret_path(get_settings()).exists()
    assert discover(api_key=None).json()["details"]["field"] == "api_key"
    assert discover(base_url=base + "/empty").json()["models"] == []
    for status, kind in [
        (401, "authorization"),
        (403, "authorization"),
        (404, "unsupported"),
        (405, "unsupported"),
    ]:
        failure = discover(base_url=f"{base}/status/{status}")
        assert failure.json()["details"] == {"kind": kind, "upstream_status": status}
        assert KEY not in failure.text
    for root, kind in [
        ("plaintext", "invalid_json"),
        ("misformed", "invalid_response"),
        ("huge", "response_limit"),
        ("bad-row", "invalid_response"),
        ("echo", "invalid_response"),
    ]:
        failure = discover(base_url=f"{base}/{root}")
        assert failure.status_code == 502 and failure.json()["details"]["kind"] == kind
        assert KEY not in failure.text
    redirect = discover(base_url=base + "/redirect")
    assert redirect.json()["details"]["kind"] == "redirect"
    assert not any(route == "/capture" for route, _, _ in calls)
    invalid = discover(models_url=f"{base}/models?api_key={KEY}")
    assert invalid.status_code == 422 and KEY not in invalid.text

    official = discover(models_url=base + "/official/catalog", discovery_format="dashscope")
    assert official.status_code == 200, official.text
    models = official.json()["models"]
    assert [p["model_id"] for p in models] == [SNAPSHOT, "official-second"]
    assert (
        models[0]["max_output_tokens"] == 512
        and models[0]["specification_source"] == "provider_catalog"
    )
    assert models[0]["supports_tools"] == "unknown"
    assert [q["page_no"][0] for route, _, q in calls if route == "/official/catalog"] == ["1", "2"]
    with monkeypatch.context() as limit:
        limit.setattr("agent_workbench.config.model_discovery.MAX_MODELS", 1)
        limited = discover().json()
        assert limited["truncated"] and len(limited["models"]) == 1

    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        connection = discover(base_url=f"http://127.0.0.1:{unavailable.getsockname()[1]}")
    assert connection.json()["details"]["kind"] == "connection"
    monkeypatch.setattr("agent_workbench.config.model_discovery.TOTAL_TIMEOUT", 0.15)
    timeout = discover(base_url=base + "/slow/v1")
    assert timeout.status_code == 504 and timeout.json()["details"]["kind"] == "timeout"

    manual = client.post(
        API + "/providers",
        json=provider_payload(
            base + "/status/404",
            models=[{"model_id": "hand-entered/latest"}],
            default_model="hand-entered/latest",
        ),
        headers=headers,
    )
    assert manual.status_code == 200 and manual.json()["default_model"] == "hand-entered/latest"
    previous_calls = len(calls)
    blocked = discover(
        api_key=None,
        saved_provider_id="office-gateway",
        base_url=base + "/status/404",
        models_url=base.replace("127.0.0.1", "localhost") + "/custom",
    )
    assert blocked.status_code == 422 and len(calls) == previous_calls


@pytest.fixture
def isolated_console(settings, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    data = tmp_path / "console-data"
    secrets = tmp_path / "local-vault" / "secrets.json"
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "WORKBENCH_DATABASE_URL": settings.database_url,
        "WORKBENCH_DATA_ROOT": str(data),
        "WORKBENCH_SECRET_STORE_PATH": str(secrets),
        "WORKBENCH_ALLOWED_ORIGINS": origin,
        "WORKBENCH_SESSION_SECRET": "fixture-console-session",
        "XIAOMI_API_KEY": "",
        "DASHSCOPE_API_KEY": "",
        "DASHSCOPE_BASE_URL": "",
    }
    assert "workbench_test_" in env["WORKBENCH_DATABASE_URL"]
    with (
        (tmp_path / "console.log").open("wb") as log,
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_workbench.cli",
                "api",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=log,
        ) as process,
    ):
        try:
            with httpx.Client(timeout=0.25, trust_env=False) as client:
                for _ in range(160):
                    if process.poll() is not None:
                        pytest.fail("Isolated console exited during startup")
                    with suppress(httpx.HTTPError):
                        if client.get(origin + "/console/").status_code == 200:
                            break
                    time.sleep(0.15)
                else:
                    pytest.fail(
                        "Isolated console unavailable; build the web bundle before acceptance"
                    )
            yield origin, data, secrets
        finally:
            process.terminate()
            process.wait(timeout=15)


def test_model_settings_browser(isolated_console, directory_server):
    pytest.importorskip("playwright")
    loop_factory = getattr(asyncio, "ProactorEventLoop", None)
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(_browser_flow(isolated_console, directory_server))


async def _browser_flow(isolated_console, directory_server):
    from playwright.async_api import async_playwright, expect

    origin, data, secrets = isolated_console
    endpoint, calls = directory_server
    screenshots_dir = os.environ.get("REXAK_MODEL_SETTINGS_SCREENSHOTS")

    async def screenshot(page, name: str):
        if screenshots_dir:
            folder = Path(screenshots_dir)
            folder.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=folder / name, animations="disabled")

    async with async_playwright() as playwright:
        channel = "msedge" if sys.platform == "win32" else None
        browser = await playwright.chromium.launch(channel=channel)
        page = await browser.new_page(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        await page.goto(origin + "/console/")
        await page.get_by_role("button", name="模型设置", exact=True).click()
        button = page.get_by_role("button", name="新增提供商")
        await expect(button).to_be_visible()
        await screenshot(page, "01-providers.png")
        await button.click()
        await page.get_by_role("button", name="保存提供商").click()
        await expect(page.get_by_role("alert")).to_contain_text("请输入 Provider ID")
        await page.get_by_label("Provider ID").fill("office-gateway")
        await page.get_by_label("显示名称").fill("办公 模型网关")
        await page.get_by_label("API Base URL").fill(endpoint + "/slow/v1")
        await page.get_by_label("API Key").fill(KEY)
        await screenshot(page, "02-provider-editor.png")
        await page.get_by_role("button", name="获取可用模型").click()
        await expect(page.get_by_text("正在获取模型…")).to_be_visible()
        await asyncio.sleep(0.15)
        await page.get_by_role("button", name="取消查询").click()
        await expect(page.get_by_text("已取消目录查询。")).to_be_visible()
        await page.get_by_label("API Base URL").fill(endpoint + "/v1/chat/completions/")
        await page.get_by_role("button", name="获取可用模型").click()
        picker = page.get_by_role("dialog", name="选择要添加的模型")
        await expect(picker).to_be_visible()
        await expect(picker.get_by_role("button", name="添加所选")).to_be_disabled()
        await page.keyboard.press("Escape")
        await expect(picker).not_to_be_visible()
        await expect(page.get_by_label("显示名称")).to_have_value("办公 模型网关")
        await expect(page.get_by_label("API Key")).to_have_value(KEY)
        await page.get_by_role("button", name="获取可用模型").click()
        await picker.get_by_role("button", name="全选筛选结果").click()
        await expect(picker.get_by_role("status")).to_have_text("已选 2 项")
        await picker.get_by_role("button", name="取消全选").click()
        await picker.get_by_label("搜索可用模型").fill("1017")
        await expect(picker.get_by_role("checkbox")).to_have_count(1)
        await picker.get_by_role("checkbox", name=SNAPSHOT).focus()
        await page.keyboard.press("Space")
        await expect(picker.get_by_role("status")).to_have_text("已选 1 项")
        await screenshot(page, "03-discovery-picker.png")
        await picker.get_by_label("搜索可用模型").clear()
        await picker.get_by_role("checkbox", name="org/chat-model").check()
        await picker.get_by_role("button", name="添加所选").click()
        await expect(page.get_by_text("已加入 2 个模型，保存后生效。")).to_be_visible()
        assert not (data / "settings" / "models.json").exists()
        assert not secrets.with_name("model-secrets.json").exists()

        await page.get_by_role("button", name="编辑模型 " + SNAPSHOT).click()
        editor = page.get_by_role("dialog", name="编辑模型")
        await editor.get_by_label("显示名称").fill("Flash 快照")
        await editor.get_by_text("高级参数与能力", exact=True).click()
        await editor.get_by_label("上下文窗口").fill("32000")
        await editor.get_by_label("最大输出长度").fill("4096")
        await editor.get_by_label("单次请求输出预算").fill("512")
        await editor.get_by_label("Function Calling").select_option("supported")
        await editor.get_by_role("checkbox", name="设为默认模型").check()
        await screenshot(page, "04-model-capabilities.png")
        await editor.get_by_role("button", name="应用修改").click()
        await page.get_by_role("button", name="保存提供商").click()
        await expect(page.get_by_text("已保存模型设置。")).to_be_visible()
        await page.reload()
        await page.get_by_role("button", name="模型设置", exact=True).click()
        settings_dialog = page.get_by_role("dialog", name="模型设置")
        await expect(settings_dialog.get_by_text("办公 模型网关", exact=True)).to_be_visible()
        persisted = json.loads((data / "settings" / "models.json").read_text(encoding="utf-8"))
        assert (
            persisted["default_provider"] == "office-gateway"
            and persisted["default_model"] == SNAPSHOT
        )
        assert KEY not in json.dumps(persisted)
        await page.get_by_role("button", name="编辑 办公 模型网关").click()
        await page.get_by_role("button", name="获取可用模型").click()
        await expect(picker.get_by_role("checkbox", name=SNAPSHOT, exact=False)).to_be_disabled()
        assert calls[-1] == ("/v1/models", f"Bearer {KEY}", {})
        await picker.get_by_role("button", name="取消", exact=True).click()

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.get_by_label("API Base URL").fill(endpoint + "/status/404")
        await page.get_by_role("button", name="获取可用模型").click()
        await expect(page.get_by_role("alert")).to_contain_text("HTTP 404")
        await page.get_by_role("button", name="手动添加模型").click()
        editor = page.get_by_role("dialog", name="手动添加模型")
        await editor.get_by_label("真实 Model ID").fill(SNAPSHOT)
        await editor.get_by_role("button", name="添加模型").click()
        await expect(editor.get_by_role("alert")).to_contain_text("已添加")
        await editor.get_by_label("真实 Model ID").fill("vendor/hand-entered")
        await editor.get_by_role("button", name="添加模型").click()
        await page.get_by_label("搜索已添加模型").fill("hand-entered")
        await expect(page.get_by_text("vendor/hand-entered", exact=True)).to_be_visible()
        await page.get_by_text("vendor/hand-entered", exact=True).scroll_into_view_if_needed()
        await screenshot(page, "05-narrow-editor.png")
        assert await page.locator(".model-configuration").evaluate(
            "(e) => e.scrollWidth <= e.clientWidth + 1"
        )
        await page.get_by_role("button", name="移除模型 vendor/hand-entered").click()
        await expect(page.get_by_text("没有匹配的型号。")).to_be_visible()
        await browser.close()
