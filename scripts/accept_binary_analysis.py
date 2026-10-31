"""独立空库中的真实样本验收；--serve 供浏览器，--real-model 显式启用一次模型验收。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import get_settings
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8


def retain_model_state(report: dict[str, Any], state: dict[str, Any]) -> None:
    """逐个图状态保留可见协议证据；不保存请求正文或隐藏推理。"""
    from langchain_core.messages import AIMessage, ToolMessage

    from agent_workbench.evidence.artifacts import redact_text

    tools = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    report["model_tools"] = []
    for message in tools:
        try:
            parsed = json.loads(str(message.content))
        except (ValueError, TypeError):
            parsed = {}
        report["model_tools"].append(
            {
                "name": message.name,
                "bytes": len(str(message.content).encode("utf-8")),
                "status": parsed.get("status") if isinstance(parsed, dict) else "non_object",
                "reference": parsed.get("reference") if isinstance(parsed, dict) else None,
            }
        )
    observations = "\n".join(str(m.content) for m in tools)
    report["model_observed"] = {
        key: key in observations
        for key in ("/api.php?op=archive&file=", "127.0.0.1:2333", "&maintainer=1")
    }
    models = [m for m in state["messages"] if isinstance(m, AIMessage)]
    report["model_decisions"] = [
        {
            "tools": [call["name"] for call in m.tool_calls],
            "has_answer": bool(m.content) and not m.tool_calls,
        }
        for m in models
    ]
    report["model_usage"] = [m.usage_metadata for m in models if m.usage_metadata]
    if models and not models[-1].tool_calls:
        report["model_answer"] = redact_text(str(models[-1].content))


async def accept(
    args: argparse.Namespace, report: dict[str, Any], binding: Any, secret: str
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from agent_workbench.api.app import create_app
    from agent_workbench.contracts.identity import ScopeSnapshot
    from agent_workbench.domain.binary.service import BinaryService
    from agent_workbench.domain.scope import ScopePolicy
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.persistence.db import Database
    from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
    from agent_workbench.persistence.tables import AttachmentRow, ToolOperationRow

    cfg = get_settings()
    database = Database(cfg)
    await database.start()
    try:
        async with database.transaction() as db:
            project = await ProjectRepository(db).create(
                owner_id=cfg.owner_id,
                name="binary-isolated-acceptance",
                target_description="仅静态分析；不访问目标",
                default_scope={},
            )
            session = await SessionRepository(db).create(
                project_id=project.id,
                title="二进制隔离验收",
                scope_snapshot=ScopeSnapshot(mode="instruction").model_dump(),
                policy_snapshot={},
            )
        report["session_id"] = str(session.id)
        if args.serve:
            import uvicorn

            stop = Path(args.output).parent / "stop-ui-fixture"
            stop.unlink(missing_ok=True)
            report.update(
                status="serving",
                url="http://127.0.0.1:8778/console/",
                stop_file=str(stop.resolve()),
            )
            save(args, report)
            server = uvicorn.Server(
                uvicorn.Config(create_app(), host="127.0.0.1", port=8778, log_level="warning")
            )

            def watch() -> None:
                deadline = time.monotonic() + 1800
                while not stop.exists() and time.monotonic() < deadline:
                    time.sleep(1)
                server.should_exit = True

            threading.Thread(target=watch, daemon=True).start()
            await server.serve()
            report["status"] = "stopped"
            return
        path = Path(args.sample)
        if not path.is_file():
            report.update(status="sample_unavailable")
            return
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        # 仅验收脚本包含预期，不进入正式提示或运行时工具。
        if digest != "5c09f586174c0afd563eea3f69699a94e3447037e47590e0c28fe2f003cc93ca":
            report.update(status="sample_hash_mismatch", sha256=digest)
            return
        report.update(sha256=digest, bytes=len(raw))
        with TestClient(create_app()) as client:
            headers = {
                "X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"],
                "Idempotency-Key": str(uuid4()),
            }
            response = client.post(
                f"/api/sessions/{session.id}/attachments",
                params={"filename": "program.exe"},
                content=raw,
                headers=headers,
            )
            response.raise_for_status()
            attachment = response.json()
            report["upload"] = {
                k: attachment[k] for k in ("size", "sha256", "content_type", "parse_status")
            }
            aid = UUID(attachment["attachment_id"])
            client.post(
                f"/api/sessions/{session.id}/messages",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"content": "只读静态分析上传程序", "attachment_ids": [str(aid)]},
            ).raise_for_status()
            assert (
                client.get(f"/api/sessions/{session.id}/attachments/{aid}/content").content == raw
            )
        # 新页面 Cookie 从真实持久化会话中恢复附件。
        with TestClient(create_app()) as client:
            client.post("/api/session")
            history = client.get(f"/api/sessions/{session.id}/conversation").json()
            report["history_restored"] = any(
                a["attachment_id"] == str(aid)
                for entry in history["entries"]
                for a in entry.get("attachments", [])
            )
        store = ArtifactStore(cfg)
        service = BinaryService(database, session.id, cfg, store)
        profile = await service.query(aid, "profile")
        report["profile"] = profile
        report["tool_checks"] = {}
        for query in ("http://127.0.0.1:2333", "/api.php?op=archive&file=", "&maintainer=1"):
            result = await service.query(aid, "strings", query=query, limit=3)
            report["tool_checks"][query] = {
                "status": result["status"],
                "total": result.get("total"),
                "locations": [
                    {k: hit[k] for k in ("file_offset", "rva", "va", "section", "reference")}
                    for hit in result.get("items", [])
                ],
            }
        if args.real_model:
            from langchain.agents import create_agent
            from langchain_core.messages import HumanMessage
            from langgraph.errors import GraphRecursionError

            from agent_workbench.agent.context import RunContext
            from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
            from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
            from agent_workbench.agent.prompts.system import SYSTEM_PROMPT
            from agent_workbench.models.provider import build_chat_model
            from agent_workbench.tools.attachments import ATTACHMENT_TOOLS
            from agent_workbench.tools.binary import BINARY_TOOLS

            class OneSecret:
                def resolve(self, secret_ref: str) -> str:
                    del secret_ref
                    return secret

                def exists(self, secret_ref: str) -> bool:
                    del secret_ref
                    return bool(secret)

            physical = []

            async def on_request(_request):
                physical.append({"attempt": len(physical) + 1})
                report["model_physical_requests"] = len(physical)
                save(args, report)

            async with httpx.AsyncClient(event_hooks={"request": [on_request]}) as http:
                model = build_chat_model(
                    replace(binding, extra={**(binding.extra or {}), "http_async_client": http}),
                    secret_store=OneSecret(),
                    streaming=False,
                )
                box: Any = SimpleNamespace(
                    db=database,
                    settings=cfg.model_copy(update={"session_graph_enabled": False}),
                    artifacts=store,
                    session_id=session.id,
                    scope=ScopePolicy(ScopeSnapshot(mode="instruction")),
                    working_context=None,
                    graph_review_window_id=None,
                    graph_sync_snapshot=None,
                    context_manifest=None,
                )
                agent = create_agent(
                    model=model,
                    tools=[*ATTACHMENT_TOOLS, *BINARY_TOOLS],
                    system_prompt=SYSTEM_PROMPT,
                    middleware=[
                        WorkingContextMiddleware(),
                        ContextBuilderMiddleware(binding.profile),
                    ],
                    context_schema=RunContext,
                )
                inputs: Any = {
                    "messages": [
                        HumanMessage(
                            content="分析我上传的程序附件，识别它的主要功能、技术特征，以及可能与 CTF Web 题目有关的线索。只允许静态分析，不运行文件，也不访问任何网络目标。请说明证据与不确定性。"
                        )
                    ]
                }
                try:
                    async for state in agent.astream(
                        inputs,
                        {"recursion_limit": 32},
                        context=RunContext(toolbox=box),
                        stream_mode="values",
                    ):
                        retain_model_state(report, state)
                        save(args, report)
                    report["model_status"] = "completed"
                except GraphRecursionError:
                    report.update(
                        model_status="step_limit_reached", error_type="GraphRecursionError"
                    )
                report["scope_unchanged"] = box.scope.snapshot.rules == []
        async with database.session() as db:
            source = await db.get(AttachmentRow, aid)
            assert source is not None and store.verify(source.relative_key, digest)
            report["remote_operations"] = await db.scalar(
                select(func.count())
                .select_from(ToolOperationRow)
                .where(ToolOperationRow.session_id == session.id)
            )
        report["status"] = (
            "model_not_completed"
            if args.real_model and report.get("model_status") != "completed"
            else "completed"
        )
    finally:
        await database.close()


def save(args: argparse.Namespace, report: dict[str, Any]) -> None:
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument(
        "--sample", default=r"C:\Users\HP\Desktop\Raphael_Terminal\Raphael_Terminal.exe"
    )
    parser.add_argument("--output", default="reports/binary-analysis-v1/http-model-acceptance.json")
    args = parser.parse_args()
    base = get_settings()
    binding, secret = None, ""
    if args.real_model:
        from agent_workbench.config.secrets import build_secret_store
        from agent_workbench.models.provider import ModelBinding, resolve_provider_profile

        profile = resolve_provider_profile(settings=base)
        secret = build_secret_store().resolve(profile.secret_ref)
        binding = ModelBinding(
            profile,
            request_timeout_seconds=base.model_request_timeout_seconds,
            extra={"max_tokens": 4096},
        )
    url = make_url(base.database_url)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    name = "workbench_binary_accept_" + uuid4().hex
    assert re.fullmatch(r"workbench_binary_accept_[0-9a-f]{32}", name)
    report: dict[str, Any] = {
        "database": name,
        "runner_started": False,
        "sample_executed": False,
        "sample_urls_requested": False,
        "real_model": args.real_model,
        "model_name": binding.profile.model_name if binding else None,
    }
    created = False
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        with tempfile.TemporaryDirectory(prefix="rexak-binary-accept-") as folder:
            os.environ.update(
                WORKBENCH_DATABASE_URL=url.set(database=name).render_as_string(hide_password=False),
                WORKBENCH_DATA_ROOT=folder,
                WORKBENCH_OWNER_ID="binary-isolated-acceptance",
                WORKBENCH_SECRET_STORE_PATH=str(Path(folder) / "secrets.json"),
                WORKBENCH_API_PORT="8778",
                WORKBENCH_EXECUTOR_HOST="127.0.0.1",
                WORKBENCH_EXECUTOR_PORT="1",
                XIAOMI_API_KEY="synthetic-unused",
                DASHSCOPE_API_KEY="",
                PYTHONIOENCODING="utf-8",
            )
            get_settings.cache_clear()
            subprocess.run(
                [sys.executable, "-m", "agent_workbench.cli", "db-init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
            asyncio.run(accept(args, report, binding, secret))
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        # 不打印可能带供应商正文、凭据或请求参数的异常。
    finally:
        if created:
            with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            report["isolated_database_removed"] = True
        save(args, report)
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in ("status", "model_name", "model_physical_requests", "error_type")
                    if k in report
                },
                ensure_ascii=False,
            )
        )
    if report.get("status") in {"failed", "model_not_completed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_event_loop_policy()
    main()
