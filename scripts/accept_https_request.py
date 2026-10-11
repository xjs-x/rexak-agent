"""对明确指定的目标只做 HEAD：校验开启、单次关闭、再次开启。无模型调用。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import CompletionReason, OperationStatus, RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeSnapshot
from agent_workbench.domain.instruction_scope import scope_from_instructions
from agent_workbench.domain.run_state import claim_run, create_run, set_run_status
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.client import HttpExecutorClient
from agent_workbench.execution.tunnel import SshTunnel
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import SessionRow
from agent_workbench.platform_compat import (
    configure_event_loop_policy,
    configure_local_loopback,
    configure_stdio_utf8,
)
from agent_workbench.tools.http import HttpRequestArgs, run_http_request
from agent_workbench.tools.runtime import ResourceLocks, Toolbox


async def verify(url: str, output: Path):
    settings = get_settings()
    db = Database(settings)
    await db.start()
    tunnel = SshTunnel(settings)
    executor = HttpExecutorClient(tunnel, settings=settings)
    task_id = None
    try:
        scope = scope_from_instructions(ScopeSnapshot(mode="instruction"), [url])
        async with db.transaction() as session:
            project = await ProjectRepository(session).create(
                owner_id="tls-fix-acceptance",
                name="HEAD 验收 " + uuid4().hex[:8],
                target_description="仅指定网址的 HTTPS HEAD 复测",
                default_scope=scope.model_dump(),
            )
            task = await SessionRepository(session).create(
                project_id=project.id,
                title="HTTPS HEAD 修复验收",
                scope_snapshot=scope.model_dump(),
                policy_snapshot=PolicySnapshot().model_dump(),
            )
            task_id = task.id
            run = await create_run(
                session,
                session_id=task.id,
                objective="仅 HEAD 检查 HTTPS 工具错误：" + url,
                provider_id=settings.default_provider,
                model_name=settings.xiaomi_model,
                config_snapshot={"acceptance": "https_head_no_model"},
            )
            await set_run_status(session, run_id=run.id, target=RunStatus.RUNNING)
            claimed, generation = await claim_run(
                session, run_id=run.id, owner="tls-fix-acceptance", lease_seconds=120
            )
            assert claimed
        await executor.start()
        toolbox = Toolbox(
            run_id=run.id,
            session_id=task.id,
            owner="tls-fix-acceptance",
            owner_generation=generation,
            settings=settings,
            db=db,
            scope=ScopePolicy(scope),
            executor=executor,
            artifacts=ArtifactStore(settings),
            spans=SpanRecorder(),
            locks=ResourceLocks(settings),
            sdk_retries_disabled=True,
        )
        results = []
        for index, checked in enumerate((True, False, True)):
            result = await run_http_request(
                HttpRequestArgs(method="HEAD", url=url, verify_tls=checked),
                toolbox,
                f"head-tls-{index}",
            )
            results.append(result)
        assert results[0].error and results[0].error.code == "tls_certificate_error"
        assert (
            results[1].status is OperationStatus.SUCCEEDED
            and results[1].data["tls_verified"] is False
        )
        assert results[2].error and results[2].error.code == "tls_certificate_error"
        report = {
            "url": url,
            "method": "HEAD",
            "model": "not_run",
            "results": [
                {
                    "verify_tls": checked,
                    "status": str(result.status),
                    "operation_id": str(result.operation_id),
                    "job_id": str(result.job_id),
                    "http_status": result.data.get("status_code"),
                    "tls_verified": result.data.get("tls_verified"),
                    "error_code": result.error.code if result.error else None,
                    "error_message": result.error.message if result.error else None,
                }
                for checked, result in zip((True, False, True), results, strict=True)
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        async with db.transaction() as session:
            await set_run_status(
                session,
                run_id=run.id,
                target=RunStatus.COMPLETED,
                completion_reason=CompletionReason.GOAL_ANSWERED,
            )
    finally:
        if task_id:
            async with db.transaction() as session:
                task = await session.get(SessionRow, task_id)
                if task:
                    task.status = "deleted"
        await executor.close()
        await tunnel.close()
        await db.close()


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_local_loopback()
    configure_event_loop_policy()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/acceptance-results/2026-10-11/https-head.json")
    )
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        parser.error("仅支持明确给出的 HTTPS 目标")
    asyncio.run(verify(args.url, args.output))
