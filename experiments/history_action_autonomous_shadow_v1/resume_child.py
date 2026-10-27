"""仅恢复本轮隔离测试库指定 Run；脚本模型和 MockTransport，拒绝业务库。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.engine import make_url
from tests.integration.test_autonomous_shadow import MockExecutor, NoTunnel
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import final_text, tool_call

from agent_workbench.agent import factory
from agent_workbench.config.settings import get_settings
from agent_workbench.persistence.db import Database
from agent_workbench.platform_compat import configure_event_loop_policy
from agent_workbench.runtime import runner as runner_module
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.tools.http import http_request
from agent_workbench.tools.shadow_test_profile import run_http_test_profile


async def main():
    settings = get_settings()
    url = make_url(settings.database_url)
    if url.host not in {"127.0.0.1", "localhost", "::1"} or not (url.database or "").startswith(
        "workbench_test_"
    ):
        raise ValueError("isolated_local_test_database_required")
    settings = settings.model_copy(
        update={
            "executor_id": "offline-executor",
            "history_action_shadow_enabled": True,
            "history_action_shadow_context_path": Path(sys.argv[2]),
            "history_action_shadow_grants_path": None,
            "history_action_shadow_timeout_seconds": 2,
            "segment_decision_rounds": 1,
        }
    )
    executor = MockExecutor()
    model = SteppedModel(
        steps=[
            lambda _: tool_call("run_http_test_profile", {"profile_id": "next"}, "repeat"),
            lambda _: final_text("controlled offline execution finished"),
        ]
    )
    factory.build_chat_model = lambda *_a, **_kw: model
    factory.build_tools = lambda *_a, **_kw: [run_http_test_profile, http_request]
    runner_module.HttpExecutorClient = lambda *_a, **_kw: executor
    runner_module.SshTunnel = NoTunnel
    db = Database(settings)
    await db.start()
    try:
        outcome = await AgentRunner(
            database=db, settings=settings, owner="child-restored-runner"
        ).step(only_run_id=UUID(sys.argv[1]))
        assert outcome is not None
        print(
            json.dumps(
                {
                    "status": outcome.status.value,
                    "submissions": executor.submissions,
                    "physical_jobs": len(executor.jobs),
                    "closed": executor.closed,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await db.close()
        await executor.client.aclose()


if __name__ == "__main__":
    configure_event_loop_policy()
    asyncio.run(main())
