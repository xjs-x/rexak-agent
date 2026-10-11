"""本机独立浏览器验收：自动目标、可选严格模式、断线与重新连接。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright, expect

from agent_workbench.platform_compat import configure_local_loopback, configure_stdio_utf8

OUTPUT = Path(__file__).resolve().parents[1] / "docs/acceptance-results/2026-10-11"


async def verify(base_url: str) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        writes: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                writes.append(request.url)
                if request.method == "POST" and request.url.endswith("/sessions")
                else None
            ),
        )
        await page.goto(base_url + "/console/", wait_until="domcontentloaded")
        await expect(page.locator(".connection")).to_have_text("已就绪")
        suffix = uuid4().hex[:6]
        draft = f"界面验收 {suffix}：测试 http://browser.example.test/"
        await page.get_by_label("消息", exact=True).fill(draft)
        await page.route("**/health", lambda route: route.abort())
        await page.route("**/api/**", lambda route: route.abort())
        await expect(page.locator(".connection")).to_have_text("服务已断开", timeout=15_000)
        await expect(page.get_by_label("消息", exact=True)).to_have_value(draft)
        await expect(page.get_by_role("button", name="发送消息", exact=True)).to_be_disabled()
        await page.screenshot(path=str(OUTPUT / "connection-offline.png"))
        assert writes == []
        await page.unroute("**/health")
        await page.unroute("**/api/**")
        await expect(page.locator(".connection")).to_have_text("已就绪", timeout=15_000)
        await expect(page.get_by_label("消息", exact=True)).to_have_value(draft)

        # 创建成功后，列表读取失败也不能把已创建任务误报为提交失败。
        fail_read = False

        async def fail_one_read(route):
            nonlocal fail_read
            if route.request.method == "POST":
                fail_read = True
            elif fail_read:
                fail_read = False
                await route.abort()
                return
            await route.continue_()

        await page.route("**/api/projects/*/sessions", fail_one_read)
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
        ) as pending:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        response = await pending.value
        assert response.status == 201, await response.text()
        created = await response.json()
        task_id = created["session"]["id"]
        await expect(page.get_by_label("消息", exact=True)).to_have_value("")
        await page.unroute("**/api/projects/*/sessions")
        await expect(page.locator(".connection")).to_have_text("已连接", timeout=15_000)
        assert len(writes) == 1, writes
        snap = await page.request.get(f"{base_url}/api/sessions/{task_id}/snapshot")
        assert snap.ok, await snap.text()
        scope = (await snap.json())["session"]["scope"]
        assert scope["mode"] == "instruction"
        assert scope["rules"][0]["host"] == "browser.example.test"
        assert scope["rules"][0]["port"] is None and scope["rules"][0]["scheme"] == "any"
        await page.get_by_role("button", name="任务详情", exact=True).click()
        await page.get_by_role("button", name="设置", exact=True).click()
        await expect(page.locator(".scope-view")).to_contain_text('"mode": "instruction"')
        await page.get_by_role(
            "heading", name="运行模式与目标", exact=True
        ).scroll_into_view_if_needed()
        await page.screenshot(path=str(OUTPUT / "instruction-mode.png"))
        await page.get_by_label("消息", exact=True).fill(
            "接着测试 https://second.example.test:8443/"
        )
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/messages")
        ) as pending:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        assert (await pending.value).status == 202
        snap = await page.request.get(f"{base_url}/api/sessions/{task_id}/snapshot")
        updated = (await snap.json())["session"]["scope"]
        assert updated["revision"] == 2
        assert {rule["host"] for rule in updated["rules"]} == {
            "browser.example.test",
            "second.example.test",
        }

        await page.get_by_title("新建测试任务", exact=True).click()
        strict_title = f"严格模式界面验收 {suffix}"
        await page.get_by_label("任务名称", exact=True).fill(strict_title)
        await page.get_by_label("运行模式", exact=True).select_option("scoped")
        await page.get_by_label("测试目标", exact=True).fill("检查指定网址")
        await page.get_by_label("目标网址（可选）", exact=True).fill(
            "https://strict.example.test:8443/"
        )
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
        ) as pending:
            await page.get_by_role("button", name="创建并开始", exact=True).click()
        strict_response = await pending.value
        assert strict_response.status == 201, await strict_response.text()
        strict_id = (await strict_response.json())["session"]["id"]
        snap = await page.request.get(f"{base_url}/api/sessions/{strict_id}/snapshot")
        strict_scope = (await snap.json())["session"]["scope"]
        assert strict_scope["mode"] == "scoped"
        assert (
            strict_scope["rules"][0]["scheme"] == "https"
            and strict_scope["rules"][0]["port"] == 8443
        )
        for title in (draft[:30], strict_title):
            await page.get_by_role("button", name=f"删除任务 {title}", exact=True).click()
            await (
                page.get_by_role("dialog", name="删除任务")
                .get_by_role("button", name="删除", exact=True)
                .click()
            )
            await expect(
                page.get_by_role("button", name=f"删除任务 {title}", exact=True)
            ).to_have_count(0)
        assert not errors, errors
        result = {
            "automatic_targets": "passed",
            "strict_mode": "passed",
            "offline_and_reconnect": "passed",
            "accepted_write_then_failed_read": "passed",
            "runtime_model_test": "not_run",
            "session_id": task_id,
            "strict_session_id": strict_id,
        }
        (OUTPUT / "instruction-ui.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        await browser.close()


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_local_loopback()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    args = parser.parse_args()
    if not args.base_url.startswith("http://127.0.0.1:"):
        parser.error("验收仅允许本机独立测试服务")
    asyncio.run(verify(args.base_url.rstrip("/")))
