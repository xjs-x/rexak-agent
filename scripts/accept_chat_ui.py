"""在独立 Edge 浏览器中验收本机控制台；不接管用户已打开的浏览器。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright, expect

from agent_workbench.platform_compat import configure_local_loopback, configure_stdio_utf8

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "tmp" / "ui-acceptance"


async def create(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        failures: list[str] = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(base_url + "/console/", wait_until="domcontentloaded")
        await expect(page.locator(".connection")).to_have_text("已就绪")
        await expect(page.get_by_role("button", name="发送消息")).to_be_disabled()
        await page.screenshot(path=str(OUTPUT / "new-chat.png"))
        await page.get_by_title("新建项目", exact=True).click()
        project_name = f"界面验收-{uuid4().hex[:8]}"
        await page.get_by_label("项目名称", exact=True).fill(project_name)
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/projects")
        ) as response:
            await page.get_by_role("button", name="创建项目", exact=True).click()
        project_response = await response.value
        assert project_response.status == 201, await project_response.text()
        project_id = (await project_response.json())["id"]
        await expect(page.get_by_role("dialog")).to_have_count(0)
        # 创建带显式范围的任务，并在 Runner 启动前删除。
        await page.get_by_title("新建测试任务", exact=True).click()
        await page.get_by_label("任务名称", exact=True).fill("待删除的界面验收任务")
        await page.get_by_label("测试目标", exact=True).fill("检查我提供的测试范围")
        await page.get_by_label("目标网址（可选）", exact=True).fill("https://example.test/")
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
        ) as response:
            await page.get_by_role("button", name="创建并开始", exact=True).click()
        assert (await response.value).status == 201
        await page.get_by_role("button", name="删除任务 待删除的界面验收任务", exact=True).click()
        await (
            page.get_by_role("dialog", name="删除任务")
            .get_by_role("button", name="删除", exact=True)
            .click()
        )
        await expect(
            page.get_by_role("button", name="删除任务 待删除的界面验收任务", exact=True)
        ).to_have_count(0)
        # 输入你好直接建立对话；不向模型注入测试专用系统指令。
        await page.get_by_label("消息", exact=True).fill("你好")
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
        ) as response:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        created_response = await response.value
        assert created_response.status == 201, await created_response.text()
        created = await created_response.json()
        session_id = created["session"]["id"]
        await expect(page.get_by_label("对话", exact=True)).to_contain_text("你好")
        await expect(page.get_by_label("消息", exact=True)).to_have_value("")
        # HTTP 提交失败应显示错误并保留输入，不能显示成已发送。
        await page.route(
            "**/api/sessions/*/messages",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body='{"message":"模拟连接失败"}',
            ),
        )
        await page.get_by_label("消息", exact=True).fill("失败后保留这条输入")
        await page.get_by_role("button", name="发送消息", exact=True).click()
        await expect(page.get_by_label("消息", exact=True)).to_have_value("失败后保留这条输入")
        await expect(page.get_by_role("status").filter(has_text="模拟连接失败")).to_have_count(1)
        await page.unroute("**/api/sessions/*/messages")
        await page.get_by_label("消息", exact=True).fill("")
        await page.reload(wait_until="domcontentloaded")
        await expect(page.get_by_label("对话", exact=True)).to_contain_text("你好")
        await page.screenshot(path=str(OUTPUT / "queued-chat.png"))
        assert not failures, failures
        state = {
            "project_id": project_id,
            "session_id": session_id,
            "run_id": created["run"]["id"],
            "phase": "create_passed",
        }
        (OUTPUT / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(state, ensure_ascii=False), flush=True)
        await browser.close()


async def check(base_url: str) -> None:
    state = json.loads((OUTPUT / "state.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        failures: list[str] = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(
            f"{base_url}/console/?session={state['session_id']}", wait_until="domcontentloaded"
        )
        started = time.monotonic()
        await expect(page.locator('.chat-message[data-role="assistant"]')).not_to_have_count(
            0, timeout=330_000
        )
        state["first_reply_wait_seconds"] = round(time.monotonic() - started, 2)
        await expect(page.locator(".run-badge")).to_have_text("需要你的输入", timeout=90_000)
        initial_count = await page.locator('.chat-message[data-role="assistant"]').count()
        started = time.monotonic()
        await page.get_by_label("消息", exact=True).fill("请用一句话说明你能帮助我做什么")
        await page.get_by_role("button", name="发送消息", exact=True).click()
        await expect(
            page.locator('.chat-message[data-role="assistant"]').nth(initial_count)
        ).to_be_visible(timeout=330_000)
        await expect(page.locator(".run-badge")).to_have_text("需要你的输入", timeout=90_000)
        state["followup_wait_seconds"] = round(time.monotonic() - started, 2)
        await expect(page.locator('.chat-message[data-role="assistant"]').last).to_contain_text(
            re.compile(r"\S")
        )
        final_count = await page.locator('.chat-message[data-role="assistant"]').count()
        await page.screenshot(path=str(OUTPUT / "real-chat.png"))
        await page.reload(wait_until="domcontentloaded")
        await expect(page.locator('.chat-message[data-role="assistant"]')).to_have_count(
            final_count
        )
        await expect(page.get_by_label("对话", exact=True)).to_contain_text(
            "请用一句话说明你能帮助我做什么"
        )
        # 当前任务删除之后，刷新不能恢复已移除任务。
        await page.get_by_role("button", name="删除任务 你好", exact=True).click()
        await (
            page.get_by_role("dialog", name="删除任务")
            .get_by_role("button", name="删除", exact=True)
            .click()
        )
        await expect(page.get_by_role("button", name="删除任务 你好", exact=True)).to_have_count(0)
        await expect(page.get_by_role("heading", name="新聊天", exact=True)).to_have_count(1)
        await page.reload(wait_until="domcontentloaded")
        await expect(page.get_by_role("button", name="删除任务 你好", exact=True)).to_have_count(0)
        assert not failures, failures
        state["phase"] = "real_reply_followup_reload_delete_passed"
        (OUTPUT / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(state, ensure_ascii=False), flush=True)
        await browser.close()


def main() -> None:
    configure_stdio_utf8()
    configure_local_loopback()
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("create", "check"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8770")
    args = parser.parse_args()
    if not args.base_url.startswith("http://127.0.0.1:"):
        parser.error("只允许验收本机控制台")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    asyncio.run(create(args.base_url) if args.phase == "create" else check(args.base_url))


if __name__ == "__main__":
    main()
