"""实际浏览器与独立 API 验证暂停、切换任务和普通 URL 输入。无模型调用。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright, expect

from agent_workbench.platform_compat import configure_local_loopback, configure_stdio_utf8

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/acceptance-results/2026-10-11"


async def step(run_id: str):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_workbench.cli",
        "runner",
        "--once",
        "--no-recover",
        "--only-run-id",
        run_id,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    if process.returncode:
        raise RuntimeError(stderr.decode("utf-8", errors="backslashreplace")[-1500:])
    return stdout.decode("utf-8")


async def verify(base_url: str):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(base_url + "/console/", wait_until="domcontentloaded")
        await expect(page.locator(".connection")).to_have_text("已就绪")
        suffix = uuid4().hex[:6]
        draft = f"暂停验收 {suffix}：测试 http://pause.example.test/"
        await page.get_by_label("消息", exact=True).fill(draft)
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
        ) as pending:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        created = await (await pending.value).json()
        task_id, run_id = created["session"]["id"], created["run"]["id"]
        await expect(page.get_by_role("button", name="暂停", exact=True)).to_be_enabled()
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/pause")
        ) as pending:
            await page.get_by_role("button", name="暂停", exact=True).click()
        assert (await pending.value).status == 202
        assert "paused" in await step(run_id)
        await expect(page.locator(".run-badge")).to_have_text("已暂停", timeout=15_000)
        await expect(
            page.get_by_role("status").filter(has_text="发送新指令即可继续")
        ).to_be_visible()
        await page.get_by_label("消息", exact=True).fill("暂停后仍可输入")
        await expect(page.get_by_role("button", name="发送消息", exact=True)).to_be_enabled()
        await page.screenshot(path=str(OUTPUT / "pause-ready-for-input.png"))
        await page.get_by_role("button", name="＋ 新聊天", exact=True).click()
        second_text = f"新任务验收 {suffix}：你好"
        await page.get_by_label("消息", exact=True).fill(second_text)
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
        ) as pending:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        response = await pending.value
        assert response.status == 201, await response.text()
        second = await response.json()
        old = await page.request.get(f"{base_url}/api/sessions/{task_id}/snapshot")
        assert (await old.json())["runs"][0]["status"] == "paused"
        await page.get_by_role("button", name=draft[:30], exact=True).click()
        await expect(page.locator(".run-badge")).to_have_text("已暂停")
        # 普通指令里只有 URL，没有要求“加入范围”的提示。
        await page.get_by_label("消息", exact=True).fill(
            "对这个网站继续进行测试 http://fresh.example.test/"
        )
        async with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/messages")
        ) as pending:
            await page.get_by_role("button", name="发送消息", exact=True).click()
        response = await pending.value
        assert response.status == 202, await response.text()
        assert (await response.json())["run_id"] == run_id
        snap = await page.request.get(f"{base_url}/api/sessions/{task_id}/snapshot")
        state = await snap.json()
        assert state["runs"][0]["status"] == "queued"
        assert state["session"]["scope"]["mode"] == "instruction"
        assert "fresh.example.test" in {rule["host"] for rule in state["session"]["scope"]["rules"]}
        await expect(page.get_by_label("消息", exact=True)).to_have_value("")
        for title in (draft[:30], second_text[:30]):
            await page.get_by_role("button", name=f"删除任务 {title}", exact=True).click()
            await (
                page.get_by_role("dialog", name="删除任务")
                .get_by_role("button", name="删除", exact=True)
                .click()
            )
            await expect(
                page.get_by_role("button", name=f"删除任务 {title}", exact=True)
            ).to_have_count(0)
        for item in (run_id, second["run"]["id"]):
            assert "cancelled" in await step(item)
        assert not failures, failures
        result = {
            "pause": "passed",
            "new_chat_while_paused": "passed",
            "message_resumes_paused_run": "passed",
            "normal_url_applied": "passed",
            "model_test": "not_run",
            "session_id": task_id,
            "run_id": run_id,
        }
        (OUTPUT / "pause-ui.json").write_text(
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
        parser.error("只允许本机独立验收服务")
    asyncio.run(verify(args.base_url.rstrip("/")))
