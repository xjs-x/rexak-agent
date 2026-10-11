"""浏览器能力的**实机验证**（在执行岛上运行，用真实 Chromium）。

覆盖 BUILD_PLAN §10 的核心承诺：

1. 无头浏览器能真的打开页面、拿到标题与最终 URL；
2. **逐请求范围门**：页面向范围外主机发起的子资源 / fetch 会被阻断 ——
   只检查导航是挡不住 `fetch('http://elsewhere/')` 的；
3. 越界导航在**派发前**就被拒（请求根本没发出）；
4. 快照给出的 `selector` 可以直接用来点击 / 填写（不需要猜 CSS）；
5. 截图落成工件并带 sha256；
6. **身份隔离**：一个身份登录后，另一个身份**不带**它的 Cookie。

依赖：执行岛本机 `127.0.0.1:8899` 上的 fixture 目标（`start-target.sh`）。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from conftest import DATA_ROOT, target_is_up

HEADERS: dict[str, str] = {}
TARGET = "http://127.0.0.1:8899"

SCOPE = {
    "revision": 1,
    "rules": [
        {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 8899,
            "include_subdomains": False,
            "allow_private": True,
        }
    ],
    "mappings": [],
    "denied_metadata_hosts": [],
}


@pytest.fixture(autouse=True)
def _headers(auth_headers: dict[str, str]) -> None:
    """令牌由 conftest 统一提供，避免多文件各设一份互相覆盖。"""
    HEADERS.clear()
    HEADERS.update(auth_headers)


pytestmark = pytest.mark.skipif(
    not target_is_up(),
    reason="fixture 目标未运行：先执行 ~/rexak-agent-executor/start-target.sh",
)


def _browse(
    client: Any,
    action: str,
    identity: str,
    *,
    scope_override: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    body = {
        "operation_id": str(uuid.uuid4()),
        "kind": "browser",
        "request_digest": f"browser-{uuid.uuid4().hex}",
        "scope_revision": 1,
        "scope": scope_override or SCOPE,
        "payload": {"action": action, "identity_id": identity, **fields},
    }
    response = client.post("/jobs", json=body, headers=HEADERS)
    assert response.status_code in {200, 202}, response.text
    job_id = response.json()["job_id"]

    snapshot: dict[str, Any] = {}
    for _ in range(200):
        snapshot = client.get(f"/jobs/{job_id}", headers=HEADERS).json()
        if snapshot["state"] in {"succeeded", "failed", "cancelled", "unknown"}:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"浏览器动作超时未结束：{snapshot}")

    payload = json.loads(
        client.get(f"/artifacts/{job_id}/result.json", headers=HEADERS).content.decode("utf-8")
    )
    payload["_state"] = snapshot["state"]
    payload["_job_id"] = job_id
    payload["_error_code"] = snapshot.get("error_code")
    return payload


def test_capabilities_report_a_real_browser(client: Any) -> None:
    """能力预检必须基于**存在可执行文件**，而不是"playwright 包装了"。"""
    capabilities = client.get("/capabilities", headers=HEADERS).json()
    assert capabilities["browser_available"] is True
    assert capabilities["browser_version"], capabilities
    assert any(
        name in str(capabilities["browser_version"]).lower()
        for name in ("chromium", "chrome", "edge", "msedge")
    )


def test_navigate_reads_title_and_final_url(client: Any) -> None:
    identity = f"nav-{uuid.uuid4().hex[:8]}"
    payload = _browse(client, "navigate", identity, url=f"{TARGET}/")

    assert payload["_state"] == "succeeded", payload
    result = payload["result"]
    assert result["status"] == 200
    assert result["title"] == "Fixture Shop"
    assert result["final_url"].rstrip("/") == TARGET
    assert result["final_url_out_of_scope"] is False
    assert result["blocked_request_count"] == 0


def test_snapshot_selectors_are_directly_usable(client: Any) -> None:
    """快照给出的 selector 必须能直接用 —— 模型不该自己猜 CSS。"""
    identity = f"form-{uuid.uuid4().hex[:8]}"
    assert _browse(client, "navigate", identity, url=f"{TARGET}/form")["_state"] == "succeeded"

    snapshot = _browse(client, "snapshot", identity)
    assert snapshot["_state"] == "succeeded", snapshot
    interactive = snapshot["result"]["interactive"]
    assert interactive, "表单页应该能列出可交互元素"

    by_tag: dict[str, dict[str, Any]] = {}
    for item in interactive:
        by_tag.setdefault(item["tag"], item)
    assert "input" in by_tag and "button" in by_tag

    fill = _browse(client, "fill", identity, selector=by_tag["input"]["selector"], value="hello")
    assert fill["_state"] == "succeeded", fill
    assert fill["result"]["selector"] == by_tag["input"]["selector"]

    click = _browse(client, "click", identity, selector=by_tag["button"]["selector"])
    assert click["_state"] == "succeeded", click

    text = _browse(client, "text", identity)
    assert "已提交：hello" in Path(_read_page_text(client, text)).read_text(encoding="utf-8")


def _read_page_text(client: Any, payload: dict[str, Any]) -> str:
    """把 page.txt 从执行岛取到本地临时文件，返回本地路径。"""
    job_id = payload["_job_id"]
    response = client.get(f"/artifacts/{job_id}/page.txt", headers=HEADERS)
    assert response.status_code == 200, response.text
    target = DATA_ROOT / f"page-{job_id}.txt"
    target.write_bytes(response.content)
    return str(target)


def test_out_of_scope_navigation_is_rejected_before_request(client: Any) -> None:
    """越界导航在派发前就被拒 —— 连请求都不该发出。"""
    identity = f"oos-{uuid.uuid4().hex[:8]}"
    payload = _browse(client, "navigate", identity, url="http://example.invalid/")

    assert payload["_state"] == "failed", payload
    assert payload["error"]["code"] == "scope_violation", payload["error"]


def test_out_of_scope_subresources_are_blocked_per_request(client: Any) -> None:
    """**关键用例**：页面自己发起的越界请求也必须被逐条阻断。

    只检查导航挡不住 `<img src=外部>` 或 `fetch('http://elsewhere/')`。
    """
    identity = f"sub-{uuid.uuid4().hex[:8]}"
    payload = _browse(client, "navigate", identity, url=f"{TARGET}/outbound")

    assert payload["_state"] == "succeeded", payload
    result = payload["result"]
    assert result["blocked_request_count"] >= 1, f"页面向范围外发起了请求但没被阻断：{result}"
    blocked = result["blocked_requests"]
    assert all("example.invalid" in item["url"] for item in blocked), blocked
    # 导航本身在范围内，所以不该被判成越界导航
    assert result["final_url_out_of_scope"] is False


def test_screenshot_artifact_has_hash(client: Any) -> None:
    identity = f"shot-{uuid.uuid4().hex[:8]}"
    _browse(client, "navigate", identity, url=f"{TARGET}/")

    payload = _browse(client, "screenshot", identity)
    assert payload["_state"] == "succeeded", payload
    result = payload["result"]
    assert result["screenshot_bytes"] > 1000

    response = client.get(f"/artifacts/{payload['_job_id']}/screenshot.png", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("image/png")
    digest = hashlib.sha256(response.content).hexdigest()
    assert digest == result["screenshot_sha256"], "两侧哈希必须一致"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_download_artifact_is_retrievable_under_nested_path(client: Any) -> None:
    identity = f"download-{uuid.uuid4().hex[:8]}"
    assert _browse(client, "navigate", identity, url=f"{TARGET}/")["_state"] == "succeeded"
    result = _browse(client, "download", identity, selector="#download-sample")
    assert result["_state"] == "succeeded", result
    response = client.get(f"/artifacts/{result['_job_id']}/downloads/sample.txt", headers=HEADERS)
    assert response.status_code == 200
    assert response.content.decode("utf-8") == "本地下载样本\n"


def test_identity_sessions_are_isolated(client: Any) -> None:
    """**关键用例**：一个身份登录后，另一个身份不应带上它的 Cookie。"""
    alice = f"alice-{uuid.uuid4().hex[:8]}"
    bob = f"bob-{uuid.uuid4().hex[:8]}"

    login = _browse(client, "navigate", alice, url=f"{TARGET}/login?user=alice")
    assert login["_state"] == "succeeded", login

    # alice 已登录：受保护页面应 200
    alice_profile = _browse(client, "navigate", alice, url=f"{TARGET}/private/profile")
    assert alice_profile["_state"] == "succeeded", alice_profile
    assert alice_profile["result"]["status"] == 200

    # bob 从没登录：同样的地址应 401（说明 Cookie 没有串）
    bob_profile = _browse(client, "navigate", bob, url=f"{TARGET}/private/profile")
    assert bob_profile["_state"] == "succeeded", bob_profile
    assert bob_profile["result"]["status"] == 401, (
        "另一身份的 Cookie 泄漏了：bob 不该继承 alice 的会话"
    )


def test_login_probe_and_browser_to_http_cookie_transfer(
    client: Any, auth_headers: dict[str, str]
) -> None:
    """凭据只进执行动作；登录探针通过后，同身份 HTTP 能访问受保护页面。"""
    from test_http_session_jobs import _request

    alice = f"form-alice-{uuid.uuid4().hex[:8]}"
    bob = f"form-bob-{uuid.uuid4().hex[:8]}"
    login = _browse(
        client,
        "login",
        alice,
        login_url=f"{TARGET}/login-form",
        username="alice",
        password="alice-pw",
        username_selector="#username",
        password_selector="#password",
        submit_selector="#submit",
        probe_url=f"{TARGET}/login-status",
        logged_in_selector="#logged-in",
    )
    assert login["_state"] == "succeeded", login
    assert login["result"]["authenticated"] is True
    assert _request(client, auth_headers, alice, f"{TARGET}/private/profile") == 200
    assert _request(client, auth_headers, bob, f"{TARGET}/private/profile") == 401

    import workbench_executor.api as api_module

    record = api_module.get_state().ledger
    import asyncio

    saved = asyncio.run(record.get(login["_job_id"]))
    assert saved is not None
    assert "alice-pw" not in json.dumps(saved.payload)


def test_explicit_http_mapping_preserves_logical_host_and_session(
    client: Any, auth_headers: dict[str, str]
) -> None:
    """逻辑目标映射到执行岛本机后，浏览器仍能登录并保持原始 Host 语义。"""
    mapped_scope = {
        **SCOPE,
        "mappings": [
            {
                "from_host": "fixture.test",
                "from_port": 8899,
                "to_host": "127.0.0.1",
                "to_port": 8899,
                "host_header": "fixture.test:8899",
            }
        ],
    }
    identity = f"mapped-{uuid.uuid4().hex[:8]}"
    result = _browse(
        client,
        "navigate",
        identity,
        scope_override=mapped_scope,
        url="http://fixture.test:8899/login?user=alice",
    )
    assert result["_state"] == "succeeded", result
    assert result["result"]["status"] == 200
    assert result["result"]["final_url"].startswith("http://fixture.test:8899/")
    from test_http_session_jobs import _request

    assert (
        _request(
            client,
            auth_headers,
            identity,
            "http://fixture.test:8899/private/profile",
            mapped_scope,
        )
        == 200
    )


def test_browser_sessions_endpoint_lists_identities(client: Any) -> None:
    identity = f"sess-{uuid.uuid4().hex[:8]}"
    created = client.post("/browser-sessions", json={"identity_id": identity}, headers=HEADERS)
    assert created.status_code == 200, created.text
    assert created.json()["identity_id"] == identity

    listed = client.get("/browser-sessions", headers=HEADERS).json()
    assert listed["browser_alive"] is True
    assert identity in [item["identity_id"] for item in listed["sessions"]]

    # 幂等：重复创建返回同一个会话，不会重建导致登录态丢失
    again = client.post("/browser-sessions", json={"identity_id": identity}, headers=HEADERS)
    assert again.status_code == 200
    assert again.json()["identity_id"] == identity

    deleted = client.delete(f"/browser-sessions/{identity}", headers=HEADERS)
    assert deleted.json()["closed"] is True


def test_session_state_is_persisted_for_restart(client: Any) -> None:
    """关闭会话时要把 storage_state 落盘，"重启后登录态还在"才不是空话。"""
    identity = f"persist-{uuid.uuid4().hex[:8]}"
    _browse(client, "navigate", identity, url=f"{TARGET}/login?user=alice")
    client.delete(f"/browser-sessions/{identity}", headers=HEADERS)

    state_file = DATA_ROOT / "browser-states" / f"{identity}.json"
    assert state_file.exists(), "关闭会话时必须写出 storage_state"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert any(item.get("name") == "fixture_session" for item in state.get("cookies", [])), state


def test_click_with_text_locator_works(client: Any) -> None:
    identity = f"text-{uuid.uuid4().hex[:8]}"
    _browse(client, "navigate", identity, url=f"{TARGET}/login?user=alice")
    payload = _browse(client, "click", identity, text="我的资料")
    assert payload["_state"] == "succeeded", payload
    assert "/private/profile" in payload["result"]["final_url"]
