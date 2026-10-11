"""探测：Playwright 能否驱动系统 Chromium（不下载 playwright 自带浏览器）。

用于决定 P4 的落地方式：如果这条路通，就不需要 `playwright install`
（岛上 sudo 需要密码，也无法用 apt 装依赖）。
"""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

EXECUTABLE = "/usr/bin/chromium"
TARGET = "http://127.0.0.1:8899/"


async def main() -> int:
    result: dict[str, object] = {"executable": EXECUTABLE, "target": TARGET}
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                executable_path=EXECUTABLE,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            result["launch_error"] = f"{type(exc).__name__}: {exc}"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1

        try:
            result["version"] = browser.version
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto(TARGET, wait_until="domcontentloaded", timeout=15000)
            result["status"] = response.status if response else None
            result["final_url"] = page.url
            result["title"] = await page.title()
            result["text_head"] = (await page.inner_text("body"))[:200]
            shot = await page.screenshot(type="png")
            result["screenshot_bytes"] = len(shot)
            await context.close()
        finally:
            await browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
