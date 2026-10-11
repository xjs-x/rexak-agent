"""实际浏览器验收工具进度；官方图使用可复现模型及本地 fixture 工具，无目标流量。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from agent_workbench.platform_compat import (
    configure_event_loop_policy,
    configure_local_loopback,
    configure_stdio_utf8,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "docs/acceptance-results/2026-10-11"


async def worker(run_id: UUID, signal_path: Path):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_core.tools import tool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

    from agent_workbench.agent.context import RunContext
    from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
    from agent_workbench.agent.state import AgentGraphState, next_input_state
    from agent_workbench.contracts.enums import RunStatus
    from agent_workbench.contracts.identity import ScopeSnapshot
    from agent_workbench.domain.run_state import claim_run, release_run, set_run_status
    from agent_workbench.domain.scope import ScopePolicy
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.observability.spans import SpanRecorder
    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.persistence.db import Database
    from agent_workbench.persistence.tables import RunRow, SessionRow
    from agent_workbench.runtime.runner import AgentRunner
    from agent_workbench.tools.runtime import ResourceLocks, Toolbox

    db = Database()
    await db.start()
    try:
        async with db.transaction() as session:
            run = await session.get(RunRow, run_id)
            assert run is not None
            task = await session.get(SessionRow, run.session_id)
            assert task is not None
            await set_run_status(session, run_id=run_id, target=RunStatus.RUNNING)
            acquired, generation = await claim_run(
                session, run_id=run_id, owner="tool-ui-fixture", lease_seconds=120
            )
            assert acquired
        toolbox = Toolbox(
            run_id=run.id,
            session_id=task.id,
            owner="tool-ui-fixture",
            owner_generation=generation,
            settings=db.settings,
            db=db,
            scope=ScopePolicy(ScopeSnapshot.model_validate(task.scope_snapshot)),
            executor=cast(Any, None),
            artifacts=ArtifactStore(db.settings),
            spans=SpanRecorder(),
            locks=ResourceLocks(db.settings),
            provider_id="fixture",
            model_name="scripted-ui",
            sdk_retries_disabled=True,
        )

        @tool
        async def inspect_fixture(url: str, api_key: str) -> str:
            """本地验收工具：等待人工浏览器检查完成后返回 fixture 文本；不访问 URL。"""
            async with asyncio.timeout(45):
                while not signal_path.exists():
                    await asyncio.sleep(0.1)
            return json.dumps(
                {
                    "status": "succeeded",
                    "summary": "本地验收工具返回完成",
                    "url": url,
                    "api_key": api_key,
                },
                ensure_ascii=False,
            )

        @tool
        async def inspect_error() -> str:
            """本地验收工具：返回确定的参数错误。"""
            raise ValueError("验收用参数错误")

        class StreamingFixtureModel(ScriptedChatModel):
            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                _ = stop, run_manager, kwargs
                response = self._generate(messages).generations[0].message
                response_id = f"ui-model-{len(self.seen_message_counts)}"
                if len(self.seen_message_counts) == 1:
                    for _ in range(40):
                        await asyncio.sleep(0.05)
                        yield ChatGenerationChunk(
                            message=AIMessageChunk(
                                content="",
                                id=response_id,
                                additional_kwargs={
                                    "reasoning_content": "fixture-private-reasoning"
                                },
                            )
                        )
                calls = getattr(response, "tool_calls", [])
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=response.content,
                        id=response_id,
                        tool_call_chunks=[
                            {
                                "name": call["name"],
                                "args": json.dumps(call["args"]),
                                "id": call["id"],
                                "index": index,
                            }
                            for index, call in enumerate(calls)
                        ],
                    )
                )

        model = StreamingFixtureModel(
            responses=[
                tool_call(
                    "inspect_fixture",
                    {"url": "https://fixture.example.test/", "api_key": "ui-private-value"},
                    "ui-tool-one",
                ),
                tool_call("inspect_error", {}, "ui-tool-two"),
                final_text("工具展示验收完成"),
            ]
        )
        async with open_checkpointer_connection(db.settings) as connection:
            graph = create_agent(
                model,
                tools=[inspect_fixture, inspect_error],
                middleware=[ModelObserverMiddleware(), ToolDispatchMiddleware()],
                state_schema=AgentGraphState,
                context_schema=RunContext,
                checkpointer=AsyncPostgresSaver(connection),
            )
            payload = next_input_state(
                session_id=task.id,
                run_id=run.id,
                objective=run.objective,
                user_message=run.objective,
            )
            payload["dispatch_control_version"] = int(run.control_version)
            runtime = SimpleNamespace(
                graph=graph,
                config=lambda **_kwargs: {"configurable": {"thread_id": task.thread_id}},
            )
            await AgentRunner(database=db, owner="tool-ui-fixture")._stream_once(
                cast(Any, runtime),
                RunContext(toolbox=toolbox),
                toolbox,
                task,
                run,
                payload,
            )
        async with db.transaction() as session:
            await set_run_status(session, run_id=run_id, target=RunStatus.WAITING_USER)
            await release_run(session, run_id=run_id, owner="tool-ui-fixture")
    finally:
        await db.close()


async def verify(base_url: str):
    from playwright.async_api import async_playwright, expect

    OUTPUT.mkdir(parents=True, exist_ok=True)
    signal_path = ROOT / "data/tmp" / f"tool-ui-{uuid4().hex}.ready"
    process = None
    run_id = None
    task_id = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        try:
            await page.goto(base_url + "/console/", wait_until="domcontentloaded")
            await expect(page.locator(".connection")).to_have_text("已就绪")
            await page.get_by_label("消息", exact=True).fill(
                "工具进度验收 " + uuid4().hex[:6] + "（fixture，不联网）"
            )
            async with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/sessions")
            ) as pending:
                await page.get_by_role("button", name="发送消息", exact=True).click()
            created = await (await pending.value).json()
            run_id = created["run"]["id"]
            task_id = created["session"]["id"]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                __file__,
                "--worker-run",
                run_id,
                "--signal-path",
                str(signal_path),
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            row = page.locator(".tool-activity").filter(
                has=page.get_by_text("inspect_fixture", exact=True)
            )
            await expect(
                page.locator(".chat-progress").filter(has_text="模型正在思考")
            ).to_be_visible(timeout=20000)
            assert (
                "fixture-private-reasoning" not in await page.locator(".chat-thread").inner_text()
            )
            await page.screenshot(path=str(OUTPUT / "model-thinking-progress.png"))
            await expect(row).to_be_visible(timeout=20000)
            assert not await row.get_attribute("open")
            await expect(row.locator(".tool-state")).to_have_text("正在处理")
            await expect(row.locator(".tool-detail")).to_have_count(0)
            await page.screenshot(path=str(OUTPUT / "tool-activity-collapsed.png"))
            group = page.locator(".tool-group").filter(has=row)
            await expect(group.locator(":scope > summary")).to_contain_text("1 项进行中")
            await group.locator(":scope > summary").click()
            await expect(row).to_be_hidden()
            await group.locator(":scope > summary").click()
            await expect(row).to_be_visible()
            await row.locator(":scope > summary").click()
            parameters = row.locator(".tool-section").filter(
                has=page.get_by_text("调用参数", exact=True)
            )
            output = row.locator(".tool-section").filter(
                has=page.get_by_text("返回结果", exact=True)
            )
            await expect(parameters.locator("pre")).to_be_hidden()
            await expect(output.locator("pre")).to_be_hidden()
            await parameters.locator("summary").click()
            await expect(parameters.locator("pre")).to_contain_text("https://fixture.example.test/")
            await expect(parameters.locator("pre")).to_contain_text("redacted")
            assert "ui-private-value" not in await row.inner_text()
            await group.locator(":scope > summary").click()
            signal_path.write_text("continue", encoding="utf-8")
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
            assert process.returncode == 0, stderr.decode("utf-8", errors="backslashreplace")[
                -1800:
            ]
            # 新操作与状态更新不能擅自展开已收起的整组。
            await expect(group.locator(":scope > summary")).to_contain_text("2 项", timeout=15000)
            await expect(group.locator(":scope > summary")).to_contain_text("1 项失败")
            await expect(row).to_be_hidden()
            await group.locator(":scope > summary").click()
            await expect(row.locator(".tool-state")).to_have_text("已完成", timeout=15000)
            await expect(output.locator("pre")).to_be_hidden()
            await output.locator("summary").click()
            await expect(output.locator("pre")).to_contain_text("本地验收工具返回完成")
            failed = page.locator(".tool-activity").filter(
                has=page.get_by_text("inspect_error", exact=True)
            )
            await expect(failed.locator(".tool-state")).to_have_text("失败")
            await failed.locator(":scope > summary").click()
            failed_output = failed.locator(".tool-section").filter(
                has=page.get_by_text("返回结果", exact=True)
            )
            await failed_output.locator("summary").click()
            await expect(failed_output.locator("pre")).to_contain_text("验收用参数错误")
            await page.screenshot(path=str(OUTPUT / "tool-activity-expanded.png"))
            await page.reload(wait_until="domcontentloaded")
            await expect(page.locator(".tool-activity")).to_have_count(2, timeout=15000)
            assert await page.locator(".tool-activity[open]").count() == 0
            await expect(page.locator(".message-author")).to_have_count(0)
            await expect(page.locator('.chat-message[data-role="assistant"]')).to_have_count(1)
            layout = await page.evaluate("""() => {
                const row = document.querySelector('.tool-activity > summary');
                const message = document.querySelector('.chat-message.assistant');
                const rows = [...document.querySelectorAll('.tool-activity > summary')];
                return {
                    tool_font: parseFloat(getComputedStyle(row).fontSize),
                    body_font: parseFloat(getComputedStyle(message).fontSize),
                    title_weight: getComputedStyle(row.querySelector('.tool-title')).fontWeight,
                    row_height: row.getBoundingClientRect().height,
                    row_pitch: rows[1].getBoundingClientRect().top - rows[0].getBoundingClientRect().top,
                };
            }""")
            assert layout["tool_font"] < layout["body_font"], layout
            assert layout["title_weight"] == "400", layout
            assert layout["row_height"] <= 26 and layout["row_pitch"] <= 26, layout
            await page.screenshot(path=str(OUTPUT / "tool-activity-compact.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.reload(wait_until="domcontentloaded")
            await expect(page.locator(".sidebar")).to_be_hidden()
            await expect(row.locator(".tool-state")).to_be_visible()
            await expect(failed.locator(".tool-state")).to_be_visible()
            mobile_width = await page.locator(".chat-thread").evaluate(
                "el => ({width: el.clientWidth, content: el.scrollWidth})"
            )
            await page.screenshot(path=str(OUTPUT / "tool-activity-compact-mobile.png"))
            assert mobile_width["content"] <= mobile_width["width"], mobile_width
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.reload(wait_until="domcontentloaded")
            await expect(page.locator(".tool-activity")).to_have_count(2)
            color = await page.locator(".side-heading").first.evaluate(
                "el => getComputedStyle(el).color"
            )
            assert color == "rgb(93, 98, 106)", color
            assert not failures, failures
            mark_recovery = await asyncio.create_subprocess_exec(
                sys.executable,
                __file__,
                "--fixture-recover",
                run_id,
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _recovery_output, recovery_error = await asyncio.wait_for(
                mark_recovery.communicate(), timeout=20
            )
            assert mark_recovery.returncode == 0, recovery_error.decode(
                "utf-8", errors="backslashreplace"
            )
            await expect(page.locator(".chat-progress").filter(has_text="等待恢复")).to_be_visible(
                timeout=15000
            )
            await page.screenshot(path=str(OUTPUT / "model-timeout-recovery.png"))
            async with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/resume")
            ) as resume_pending:
                await page.get_by_role("button", name="继续", exact=True).click()
            resumed = await resume_pending.value
            assert resumed.status == 202
            assert (await resumed.json())["status"] == "queued"
            await expect(
                page.locator(".chat-progress").filter(has_text="等待运行器接手")
            ).to_be_visible(timeout=15000)
            result = {
                "collapsed_default": "passed",
                "live_progress": "passed",
                "details_and_redaction": "passed",
                "error_display": "passed",
                "history_after_reload": "passed",
                "font_darkened": "passed",
                "multi_level_collapse": "passed",
                "collapsed_live_updates": "passed",
                "author_label_removed": "passed",
                "compact_layout": layout,
                "mobile_layout": mobile_width,
                "model": "scripted",
                "model_thinking_progress": "passed",
                "model_timeout_resume_without_false_unknown_job": "passed",
                "real_model": "not_run",
                "session_id": created["session"]["id"],
            }
            (OUTPUT / "tool-activity-ui.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            await page.get_by_role(
                "button", name="删除任务 " + created["session"]["title"], exact=True
            ).click()
            await (
                page.get_by_role("dialog", name="删除任务")
                .get_by_role("button", name="删除", exact=True)
                .click()
            )
        finally:
            signal_path.write_text("continue", encoding="utf-8")
            if process and process.returncode is None:
                _worker_output, worker_error = await asyncio.wait_for(
                    process.communicate(), timeout=20
                )
                if process.returncode:
                    (ROOT / "data/tmp/tool-ui/worker-error.log").write_bytes(worker_error)
            if task_id:
                ticket = await page.request.post(
                    base_url + "/api/session", headers={"Origin": base_url}
                )
                csrf = (await ticket.json())["csrf_token"]
                await page.request.delete(
                    base_url + f"/api/sessions/{task_id}",
                    headers={"Origin": base_url, "X-Workbench-CSRF": csrf},
                )
            signal_path.unlink(missing_ok=True)
            await browser.close()
            if run_id:
                cleanup = await asyncio.create_subprocess_exec(
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
                await asyncio.wait_for(cleanup.communicate(), timeout=20)


async def mark_fixture_recovering(run_id: UUID):
    from agent_workbench.contracts.enums import RunStatus
    from agent_workbench.domain.run_state import set_run_status
    from agent_workbench.persistence.db import Database
    from agent_workbench.persistence.tables import RunRow, SessionRow

    db = Database()
    await db.start()
    try:
        async with db.transaction() as session:
            run = await session.get(RunRow, run_id)
            assert run is not None and run.status == "waiting_user"
            task = await session.get(SessionRow, run.session_id)
            assert (
                task is not None
                and task.title.startswith("工具进度验收 ")
                and "fixture，不联网" in task.title
            )
            await set_run_status(session, run_id=run_id, target=RunStatus.RUNNING)
            await set_run_status(
                session,
                run_id=run_id,
                target=RunStatus.RECOVERING,
                stop_reason="模型流读取超时（本地 fixture）；点击继续可恢复",
            )
    finally:
        await db.close()


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_local_loopback()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--worker-run", type=UUID)
    parser.add_argument("--fixture-recover", type=UUID)
    parser.add_argument("--signal-path", type=Path)
    args = parser.parse_args()
    if args.fixture_recover:
        configure_event_loop_policy()
        asyncio.run(mark_fixture_recovering(args.fixture_recover))
    elif args.worker_run:
        configure_event_loop_policy()
        if args.signal_path is None:
            parser.error("worker requires signal path")
        asyncio.run(worker(args.worker_run, args.signal_path))
    else:
        if not args.base_url.startswith("http://127.0.0.1:"):
            parser.error("仅允许本机验收 API")
        asyncio.run(verify(args.base_url))
