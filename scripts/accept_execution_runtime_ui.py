"""真实 React 面板与浏览器的确定性演练；API 响应为 fixture，不连接日常库或模型。"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from playwright.async_api import async_playwright, expect

from agent_workbench.platform_compat import configure_stdio_utf8

ROOT = Path(__file__).resolve().parents[1]


async def verify(url: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    resources = [
        {"resource_id": "listener", "kind": "command", "purpose": "保留监听能力",
         "state": "running", "detail": {"completion_role": "resource"}},
        {"resource_id": "scan", "kind": "command", "purpose": "有限 fixture 计算",
         "state": "running", "detail": {"completion_role": "work"}},
        {"resource_id": "pty", "kind": "terminal", "purpose": "历史终端",
         "state": "unknown", "interaction_state": "lost", "detail": {}},
    ]
    for item in resources:
        item.update({"close_state": "open", "close_confirmed": False,
                     "sync_status": "in_sync", "quota": {"output_bytes": 1024,
                     "output_limit_bytes": 4096}, "interaction_state": item.get(
                         "interaction_state", "not_applicable")})
    duties = [{"delegation_id": "duty", "mode": "continuous", "status": "active",
               "revision": 1, "goal": "fixture 持续处理", "resource_ids": ["listener"],
               "event_filters": ["resource.watch_match"], "triggered_count": 0}]
    calls: list[str] = []
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1200})
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def fixture(route):
                request = route.request
                parsed = urlparse(request.url)
                path = parsed.path
                calls.append(request.method + " " + path)
                if path == "/api/session":
                    value = {"csrf_token": "fixture-only"}
                elif path.endswith("/duties/duty/cancel"):
                    duties[0]["status"] = "cancelled"
                    value = {"delegation_id": "duty", "status": "cancelled", "revision": 2,
                             "note": "值班已取消；资源未被关闭。"}
                elif path.endswith("/listener/close"):
                    value = {"operation_id": "fixture-close", "status": "unknown",
                             "resource_id": "listener", "process_state": "unknown",
                             "process_confirmed_exited": False}
                elif path.endswith("/output"):
                    query = parse_qs(parsed.query)
                    second = query.get("cursor") == ["page-two"]
                    value = {"resource_id": "listener", "stream": "stdout",
                             "text": "第二页中文输出" if second else "第一页中文输出",
                             "next_cursor": "end" if second else "page-two",
                             "at_current_end": second, "process_exited": False,
                             "truncated": True, "missing_ranges": [{"start": 4096, "end": 8192}],
                             "byte_count": 20, "byte_start": 0, "byte_end": 20}
                elif path.endswith("/resources"):
                    value = {"resources": resources, "count": len(resources), "duties": duties,
                             "executor_note": "确定性 fixture：没有连接真实执行端。"}
                else:
                    raise AssertionError(f"未声明的 fixture 请求：{request.method} {path}")
                await route.fulfill(json=value)

            await page.route(url.rsplit("/console/", 1)[0] + "/api/**", fixture)
            await page.goto(url, wait_until="networkidle")
            try:
                await expect(page.get_by_text("保留监听能力", exact=True)).to_be_visible()
            except AssertionError:
                print(json.dumps({"errors": errors, "calls": calls, "html": await page.content(), "url": page.url}, ensure_ascii=False))
                raise
            pending = page.locator(".resource-card").filter(has_text="有限 fixture 计算")
            listener = page.locator(".resource-card").filter(has_text="保留监听能力")
            # 检查实际 DOM 中两类资源分别位于各自标题之后。
            assert await listener.evaluate("e => {let p=e.previousElementSibling; while(p && p.tagName!=='H3')p=p.previousElementSibling; return p?.textContent}") == "保留资源（阶段结束仍存活）"
            assert await pending.evaluate("e => {let p=e.previousElementSibling; while(p && p.tagName!=='H3')p=p.previousElementSibling; return p?.textContent}") == "待完成工作"
            await listener.get_by_role("button", name="详情", exact=True).click()
            await expect(listener.get_by_text("第一页中文输出", exact=True)).to_be_visible()
            await listener.get_by_role("button", name="读取下一页", exact=True).click()
            await expect(listener.get_by_text("第二页中文输出", exact=True)).to_be_visible()
            await expect(page.get_by_text("交互：交互已丢失", exact=True)).to_be_visible()
            await page.screenshot(path=str(output / "execution-resources.png"), full_page=True)
            await page.get_by_role("button", name="取消值班", exact=True).click()
            await page.get_by_role("button", name="确认取消值班", exact=True).click()
            await expect(page.get_by_text("值班已取消；资源未被关闭。", exact=True)).to_be_visible()
            assert not any("/close" in call for call in calls)
            await listener.get_by_role("button", name="关闭资源", exact=True).click()
            await listener.get_by_role("button", name="确认关闭", exact=True).click()
            await expect(listener.get_by_text("关闭请求已发出", exact=False)).to_be_visible()
            assert await listener.get_by_text("已确认进程退出。", exact=True).count() == 0
            await page.screenshot(path=str(output / "close-unconfirmed.png"), full_page=True)
            assert not errors, errors
            (output / "ui-results.json").write_text(json.dumps({
                "status": "PASS", "checks": ["按用途分组", "输出翻页", "交互丢失",
                "取消值班不关闭资源", "关闭未确认如实展示", "无浏览器脚本异常"],
                "calls": calls, "api_mode": "fixture", "real_model": False,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            await browser.close()


def main() -> None:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("需要已安装的 Node.js 与 web 依赖")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    name = "execution-fixture-" + uuid4().hex
    html = ROOT / "web" / (name + ".html")
    entry = ROOT / "web" / (name + ".tsx")
    html.write_text('<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body><div id="root"></div><script type="module" src="/' + name + '.tsx"></script></body></html>', encoding="utf-8")
    entry.write_text('import React from "react"; import {createRoot} from "react-dom/client";'
                     'import {ExecutionPanel} from "./src/ExecutionPanel"; import "./src/style.css";'
                     'createRoot(document.getElementById("root")!).render(<main style={{padding:24}}>'
                     '<ExecutionPanel sessionId="fixture"/></main>);', encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen([node, str(ROOT / "web/node_modules/vite/bin/vite.js"),
                                    "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                                   cwd=ROOT / "web", stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/console/{name}.html"
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(80):
                if process.poll() is not None:
                    raise RuntimeError("fixture Vite 服务启动失败")
                try:
                    if client.get(url).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("fixture Vite 服务就绪超时")
        asyncio.run(verify(url, args.output.resolve()))
        print("执行面板浏览器演练 PASS")
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        html.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
