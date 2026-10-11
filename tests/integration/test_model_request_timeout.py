"""单次模型请求有墙钟边界，超时仍要结算未知用量。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.model_observer import (
    ModelObserverMiddleware,
    ModelRequestTimeout,
)
from agent_workbench.domain.run_state import create_run
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import ModelCallRow


@pytest.mark.integration
async def test_hung_model_call_times_out_and_records_unknown_usage(database, unique_name) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="model-timeout-test",
            name=unique_name,
            target_description="本地模型超时测试",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id,
            title="模型超时",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="测试模型请求墙钟边界",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )

    toolbox = SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=task.id,
        provider_id="test",
        model_name="scripted",
        sdk_retries_disabled=True,
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=RunContext(toolbox=cast(Any, toolbox))),
        state={},
        system_message=SystemMessage(content="测试"),
        messages=[],
    )

    async def never_returns(_request: Any) -> Any:
        await asyncio.sleep(60)

    with pytest.raises(ModelRequestTimeout):
        await ModelObserverMiddleware(request_timeout_seconds=0.02).awrap_model_call(
            cast(Any, request), never_returns
        )

    async with database.session() as db:
        calls = (
            (await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == run.id)))
            .scalars()
            .all()
        )
    assert len(calls) == 1
    assert calls[0].completed_at is not None
    assert calls[0].error_code == "ModelRequestTimeout"
    assert calls[0].usage_status == "unknown"


@pytest.mark.integration
async def test_cancelled_model_call_records_cancellation(database, unique_name) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="model-cancel-test",
            name=unique_name,
            target_description="本地模型取消测试",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id,
            title="模型取消",
            scope_snapshot={},
            policy_snapshot={},
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="测试模型请求取消记录",
            provider_id="test",
            model_name="scripted",
            config_snapshot={},
        )
    toolbox = SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=task.id,
        provider_id="test",
        model_name="scripted",
        sdk_retries_disabled=True,
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=RunContext(toolbox=cast(Any, toolbox))),
        state={},
        system_message=SystemMessage(content="测试"),
        messages=[],
    )
    started = asyncio.Event()

    async def never_returns(_request: Any) -> Any:
        started.set()
        await asyncio.sleep(60)

    task_call = asyncio.create_task(
        ModelObserverMiddleware().awrap_model_call(cast(Any, request), never_returns)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task_call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_call

    async with database.session() as db:
        call = (
            await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == run.id))
        ).scalar_one()
    assert call.completed_at is not None
    assert call.error_code == "CancelledError"
    assert call.usage_status == "unknown"


@pytest.mark.integration
@pytest.mark.parametrize(
    "kind", ["reasoning", "visible", "tool", "empty", "foreign", "hard", "transport"]
)
async def test_activity_renews_idle_timeout_but_not_absolute_limit(database, unique_name, kind):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="timeout-activity-test",
            name=unique_name,
            target_description="流式超时验收",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id, title="流式超时验收", scope_snapshot={}, policy_snapshot={}
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="验收流式活动边界",
            provider_id="fixture",
            model_name="scripted",
            config_snapshot={},
        )
    toolbox = SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=task.id,
        provider_id="fixture",
        model_name="scripted",
        sdk_retries_disabled=True,
    )
    context = RunContext(toolbox=cast(Any, toolbox))
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        state={},
        system_message=SystemMessage(content="验收"),
        messages=[],
    )
    calls = 0
    cancelled = False

    async def stream(_request):
        nonlocal calls, cancelled
        calls += 1
        try:
            if kind == "transport":
                raise TimeoutError("fixture stream timeout")
            for _ in range(8):
                await asyncio.sleep(0.025)
                chunk = AIMessageChunk(
                    content="" if kind not in {"visible", "hard"} else "正文", id="fixture-response"
                )
                if kind in {"reasoning", "foreign"}:
                    chunk.additional_kwargs["reasoning_content"] = "本地思考分片"
                if kind == "tool":
                    chunk.tool_call_chunks = [
                        {"name": "fixture", "args": "{}", "id": "fixture-call", "index": 0}
                    ]
                context.model_stream.observe(
                    (chunk, {"langgraph_node": "tools" if kind == "foreign" else "model"})
                )
            return ModelResponse(result=[AIMessage(content="", id="fixture-response")])
        except asyncio.CancelledError:
            cancelled = True
            raise

    observer = ModelObserverMiddleware(
        request_timeout_seconds=0.11 if kind == "hard" else 1, idle_timeout_seconds=0.08
    )
    timed_out = kind in {"empty", "foreign", "hard", "transport"}
    if timed_out:
        with pytest.raises(
            ModelRequestTimeout,
            match="保护上限"
            if kind == "hard"
            else "模型流读取超时"
            if kind == "transport"
            else "没有返回有效分片",
        ):
            await observer.awrap_model_call(cast(Any, request), stream)
        assert cancelled == (kind != "transport")
    else:
        await observer.awrap_model_call(cast(Any, request), stream)
        assert not cancelled
    assert calls == 1
    assert context.model_stream.active_call_id is None
    async with database.session() as db:
        call = (
            await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == run.id))
        ).scalar_one()
    assert call.error_code == ("ModelRequestTimeout" if timed_out else None)
    assert call.completed_at is not None
    if kind == "reasoning":
        assert call.first_token_at is None
