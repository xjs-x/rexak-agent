"""实际 SkillLibrary React 组件的浏览器演练；API 为隔离 fixture。"""

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
    card = {"skill_id": "fixture-skill", "name": "fixture-skill", "description": "中文技能描述",
            "source": "zip:fixture-skill", "enabled": True, "version_id": "v1", "label": "1.0",
            "package_hash": "a" * 64, "project_ids": [], "default_for_projects": [], "diagnostics": []}
    second = {**card, "version_id": "v2", "label": "2.0", "package_hash": "b" * 64}
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
                elif path.endswith("/skills/import"):
                    value = card
                elif path.endswith("/skills/rescan"):
                    value = {"skills": [card], "total": 1, "errors": ["bad-package: name 无效"]}
                elif path.endswith("/skills/fixture-skill") and request.method == "PATCH":
                    card.update(request.post_data_json)
                    value = {"skills": [card], "total": 1}
                elif path.endswith("/versions"):
                    value = {"skills": [card, second], "total": 2}
                elif path.endswith("/files"):
                    query = parse_qs(parsed.query)
                    name = query.get("path", ["SKILL.md"])[0]
                    value = {"path": name, "sha256": "a" * 64, "byte_size": 20,
                        "text": "中文参考资料" if name.startswith("references/") else "旧版固定指导" if "/v1/" in path else "新版指导",
                        "files": ["SKILL.md", "references/detail.md", "assets/logo.bin"], "files_total": 3}
                elif path.endswith("/sessions/fixture/skills"):
                    value = {"active": [{"skill_id": "fixture-skill", "package_hash": "a" * 64}]}
                elif path == "/api/skills":
                    value = {"root": "C:/fixture/skills", "skills": [card], "total": 1}
                else:
                    raise AssertionError(f"未声明 fixture 请求：{request.method} {path}")
                await route.fulfill(json=value)

            await page.route(url.rsplit("/console/", 1)[0] + "/api/**", fixture)
            await page.goto(url, wait_until="networkidle")
            await expect(page.get_by_text("中文技能描述", exact=True)).to_be_visible()
            await page.get_by_label("搜索技能").fill("中文")
            await page.get_by_role("checkbox").check()
            await expect(page.get_by_text("已选 1 项", exact=False)).to_be_visible()
            await page.get_by_role("button", name="版本与文件", exact=True).click()
            await expect(page.get_by_text("旧版固定指导", exact=True)).to_be_visible()
            await page.get_by_role("button", name="references/detail.md", exact=True).click()
            await expect(page.get_by_text("中文参考资料", exact=True)).to_be_visible()
            await expect(page.get_by_role("button", name="assets/logo.bin", exact=True)).to_be_visible()
            await page.get_by_label("查看技能版本").select_option("v2")
            await expect(page.get_by_text("新版指导", exact=True)).to_be_visible()
            await page.get_by_role("button", name="停用", exact=True).click()
            # 已选技能停用后仍能取消选择。
            await page.get_by_role("checkbox").uncheck()
            await page.get_by_role("button", name="启用", exact=True).click()
            await page.get_by_role("button", name="设为项目默认", exact=True).click()
            await expect(page.get_by_role("button", name="取消项目默认", exact=True)).to_be_visible()
            await page.get_by_role("button", name="刷新目录", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("bad-package")
            await page.get_by_label("技能目录路径").fill("C:/fixture/skills/fixture-skill")
            await page.get_by_role("button", name="导入目录", exact=True).click()
            await page.get_by_label("导入技能 ZIP").set_input_files({"name": "fixture.zip",
                "mimeType": "application/zip", "buffer": b"fixture-only"})
            await expect(page.get_by_role("button", name="刷新目录", exact=True)).to_be_enabled()
            await page.screenshot(path=str(output / "skill-library.png"), full_page=True)
            assert sum(call.endswith("/skills/import") for call in calls) == 2
            assert not errors, errors
            (output / "ui-results.json").write_text(json.dumps({"status": "PASS", "api_mode": "fixture",
                "checks": ["中文检索", "显式选择", "正文与附件预览", "固定版本切换", "停用后取消选择",
                           "项目默认", "刷新诊断", "目录与 ZIP 导入", "无脚本异常"],
                "calls": calls, "real_model_called": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            await browser.close()


def main() -> None:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("需要 Node.js 与 web 依赖")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    name = "skills-fixture-" + uuid4().hex
    html, entry = ROOT / "web" / (name + ".html"), ROOT / "web" / (name + ".tsx")
    html.write_text('<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body><div id="root"></div><script type="module" src="/' + name + '.tsx"></script></body></html>', encoding="utf-8")
    entry.write_text('import React,{useState} from "react";import {createRoot} from "react-dom/client";'
        'import {SkillLibrary} from "./src/SkillLibrary";import "./src/style.css";'
        'function Fixture(){const [ids,setIds]=useState<string[]>([]);return <main style={{padding:24}}>'
        '<SkillLibrary sessionId="fixture" projectId="project" selected={ids} onSelected={setIds}/></main>}'
        'createRoot(document.getElementById("root")!).render(<Fixture/>);', encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen([node, str(ROOT / "web/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1",
            "--port", str(port), "--strictPort"], cwd=ROOT / "web", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/console/{name}.html"
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(80):
                if process.poll() is not None:
                    raise RuntimeError("Vite fixture 启动失败")
                try:
                    if client.get(url).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Vite fixture 就绪超时")
        asyncio.run(verify(url, args.output.resolve()))
        print("SkillLibrary 浏览器演练 PASS")
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        html.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
