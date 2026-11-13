"""Opt-in real Docker execution through API/Runner and the production Main graph."""

import json
import os

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from agent_workbench.agent import factory
from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.persistence.tables import ToolOperationRow
from agent_workbench.runtime.runner import AgentRunner
from tests.integration.test_instruction_scope import create_task
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import final_text, tool_call

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_executor,
    pytest.mark.skipif(
        os.environ.get("KALI_DOCKER_ACCEPTANCE") != "true",
        reason="Requires the dedicated Docker Executor",
    ),
]


async def test_api_runner_main_calls_real_docker_executor(
    database, settings, monkeypatch, unique_name
):
    assert settings.executor_host == "127.0.0.1" and settings.executor_port == 2222
    assert settings.executor_id == "kali-docker-01"
    marker = "MAIN_DOCKER_CALL_CHAIN_OK"

    def finish(messages):
        outputs = [m for m in messages if isinstance(m, ToolMessage)]
        assert outputs and marker in str(outputs[-1].content), outputs
        payload = json.loads(str(outputs[-1].content))
        assert payload["status"] == "succeeded", payload
        return final_text(marker)

    model = SteppedModel(
        steps=[
            lambda _: tool_call(
                "run_command",
                {"command": f"printf {marker}", "wait_ms": 10000},
                "docker-real-main-call",
            ),
            finish,
        ]
    )
    monkeypatch.setenv("XIAOMI_API_KEY", "synthetic-never-send")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-never-send")
    monkeypatch.setattr(factory, "build_chat_model", lambda *_a, **_kw: model)
    with TestClient(create_app()) as api:
        _, run_id, _ = create_task(
            api, unique_name, objective="Run the harmless local Docker proof"
        )
        runner = AgentRunner(database=database, settings=settings, owner="docker-acceptance")
        outcome = await runner.step(only_run_id=run_id)
        assert outcome and outcome.status is RunStatus.COMPLETED, outcome
        async with database.session() as db:
            operations = list(
                (
                    await db.scalars(
                        select(ToolOperationRow).where(
                            ToolOperationRow.run_id == run_id,
                            ToolOperationRow.tool_name == "run_command",
                        )
                    )
                ).all()
            )
            assert len(operations) == 1 and operations[0].status == "succeeded"
            assert operations[0].job_id is not None
