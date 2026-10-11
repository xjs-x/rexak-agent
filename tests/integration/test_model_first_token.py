"""模型调用与图流分片通过同一消息 ID 归属。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.config.settings import get_settings
from agent_workbench.domain.run_state import create_run
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import ModelCallRow
from tests.support.scripted_model import ScriptedChatModel, final_text

pytestmark = pytest.mark.integration


class StreamingScriptedChatModel(ScriptedChatModel):
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        _ = messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(message=AIMessageChunk(content="模型", id="stream-response-1"))
        await asyncio.sleep(0)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="开始输出", id="stream-response-1")
        )


class SlowReasoningChatModel(StreamingScriptedChatModel):
    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for _ in range(6):
            await asyncio.sleep(0.025)
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id="stream-response-1",
                    additional_kwargs={"reasoning_content": "本地思考分片"},
                )
            )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="模型开始输出", id="stream-response-1")
        )


@pytest.mark.parametrize("reasoning", [False, True])
async def test_graph_stream_first_output_is_attributed_to_exact_model_call(
    database: Any, unique_name: str, reasoning: bool
) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=get_settings().owner_id,
            name=unique_name,
            target_description="本地验证",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id,
            title="流式耗时验证",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = await create_run(
            db,
            session_id=session.id,
            objective="测试首个可见分片",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )

    toolbox = cast(
        Any,
        type(
            "ToolboxStub",
            (),
            {
                "db": database,
                "run_id": run.id,
                "session_id": session.id,
                "provider_id": "test",
                "model_name": "scripted",
                "sdk_retries_disabled": True,
            },
        )(),
    )
    context = RunContext(toolbox=toolbox)
    graph = create_agent(
        model=(SlowReasoningChatModel if reasoning else StreamingScriptedChatModel)(
            responses=[final_text("模型开始输出")]
        ),
        tools=[],
        middleware=[
            ModelObserverMiddleware(
                request_timeout_seconds=1, idle_timeout_seconds=0.08 if reasoning else None
            )
        ],
        context_schema=RunContext,
    )
    chunks = 0
    async for mode, chunk in graph.astream(
        {"messages": [HumanMessage(content="你好")]},
        context=context,
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            context.model_stream.observe(chunk)
            chunks += 1
    assert chunks > 0

    async with database.session() as db:
        call = (
            await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == run.id))
        ).scalar_one()
    assert call.first_token_at is not None
    assert call.submitted_at is not None
    assert call.completed_at is not None
    assert call.submitted_at <= call.first_token_at <= call.completed_at
