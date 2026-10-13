"""捕获官方请求和真实台账，证明预算、顺序、来源及辅助调用隔离。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import Field, SecretStr
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.middleware.compaction import (
    CompactionFailed,
    GuardedSummarizationMiddleware,
)
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.config.settings import get_settings
from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.run_bundle import (
    collect_run_bundle,
    verify_run_bundle,
    write_run_bundle,
)
from agent_workbench.models.context_budget import ContextBudgetExceeded, estimate_request
from agent_workbench.models.invocation import InvocationScope, ModelRequestTimeout, invoke_attempt
from agent_workbench.models.pending_observations import (
    read_pending_observations,
    sync_model_observations,
)
from agent_workbench.models.provider import ModelBinding
from agent_workbench.models.xiaomi import XiaomiChatOpenAI
from agent_workbench.persistence.repositories import (
    ModelCallRepository,
    ProjectRepository,
    SessionRepository,
)
from agent_workbench.persistence.tables import ArtifactRow, EventRow, ModelCallRow
from tests.support.scripted_model import ScriptedChatModel, final_text
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration


async def context_for(database, unique_name):
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="budget-test",
            name=unique_name,
            target_description="本地假供应商",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id, title="预算验收", scope_snapshot={}, policy_snapshot={}
        )
        run = await create_run(
            db,
            session_id=task.id,
            objective="本地预算测试",
            provider_id="xiaomi",
            model_name="fixture",
            config_snapshot={},
        )
    toolbox = SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=task.id,
        provider_id="xiaomi",
        model_name="fixture",
        sdk_retries_disabled=True,
        context_manifest=None,
    )
    return RunContext(toolbox=cast(Any, toolbox), thread_id=task.thread_id)


@tool
def local_echo(value: str) -> str:
    """无副作用的本地回显。"""
    return value


async def test_captured_official_payload_matches_persisted_manifest_and_bundle(
    database, unique_name, tmp_path
):
    context = await context_for(database, unique_name)
    captured = []

    def fake_provider(request: httpx.Request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic-response",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "工程验收完成"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 4, "total_tokens": 127},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_provider)) as client:
        model = XiaomiChatOpenAI(
            model="fixture",
            api_key=SecretStr("synthetic"),
            base_url="http://fixture.invalid/v1",
            max_retries=0,
            use_responses_api=False,
            http_async_client=client,
        )
        assembled = build_agent_middleware(
            model, ModelBinding(profile()), get_settings(), [local_echo], default_price_catalog()
        )
        wrappers = [
            middleware
            for middleware in assembled
            if isinstance(middleware, (ContextBuilderMiddleware, ModelObserverMiddleware))
        ]
        assert [type(middleware) for middleware in wrappers] == [
            ContextBuilderMiddleware,
            ModelObserverMiddleware,
        ]
        graph = create_agent(
            model=model,
            tools=[local_echo],
            system_prompt="基础系统提示",
            state_schema=AgentGraphState,
            context_schema=RunContext,
            middleware=wrappers,
        )
        result = await graph.ainvoke(
            cast(
                Any,
                {
                    "messages": [HumanMessage(content="检查本地替身", id="u1")],
                    "objective": "只验证预算",
                    "plan_revision": 7,
                },
            ),
            context=context,
        )
    assert result["messages"][-1].content == "工程验收完成"
    assert len(captured) == 1
    wire = captured[0]
    assert (
        "基础系统提示" in wire["messages"][0]["content"]
        and "当前任务上下文" in wire["messages"][0]["content"]
    )
    assert wire["tools"][0]["function"]["name"] == "local_echo"
    budget = estimate_request(
        [HumanMessage(content="检查本地替身", id="u1")],
        SystemMessage(content=wire["messages"][0]["content"]),
        wire["tools"],
        profile(),
        model=model,
    )
    assert context.toolbox.context_manifest is not None
    assert context.toolbox.context_manifest["budget"] == budget.to_dict()
    async with database.session() as db:
        calls = (
            (
                await db.execute(
                    select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
                )
            )
            .scalars()
            .all()
        )
        event = (
            await db.execute(
                select(EventRow).where(
                    EventRow.run_id == context.toolbox.run_id, EventRow.type == "context.selected"
                )
            )
        ).scalar_one()
        artifact = await db.get(ArtifactRow, UUID(event.payload["manifest_artifact_id"]))
    assert len(calls) == 1 and calls[0].phase == "agent" and calls[0].input_tokens == 123
    assert artifact is not None
    manifest = json.loads(
        ArtifactStore(database.settings).resolve(artifact.relative_key).read_bytes()
    )
    assert manifest["source_versions"]["plan_revision"] == 7
    assert manifest["budget"]["estimated_input_tokens"] == budget.estimated_input_tokens
    assert manifest["selected_messages"][0]["id"] == "u1"
    payload = await collect_run_bundle(database, context.toolbox.run_id)
    directory = write_run_bundle(tmp_path / "bundle", payload, ArtifactStore(database.settings))
    assert verify_run_bundle(directory) == []
    index = json.loads((directory / "manifests/index.json").read_text(encoding="utf-8"))
    assert index["status"] == "available" and index["requests"][0]["path"]


async def test_latest_large_tool_result_is_archived_without_breaking_pair(database, unique_name):
    context = await context_for(database, unique_name)
    model = ScriptedChatModel(responses=[final_text("unused")])
    original = ToolMessage(content="response-part " * 4000, tool_call_id="c1", id="t1")
    request = ModelRequest(
        model=model,
        messages=[
            HumanMessage(content="目标", id="u1"),
            AIMessage(
                content="",
                id="a1",
                tool_calls=[
                    {"name": "local_echo", "args": {"value": "x"}, "id": "c1", "type": "tool_call"}
                ],
            ),
            original,
        ],
        system_message=SystemMessage(content="base"),
        tools=[local_echo],
        state=cast(Any, {"messages": [], "objective": "验证正文归档"}),
        runtime=cast(Any, SimpleNamespace(context=context)),
    )
    seen = []

    async def handler(selected) -> Any:
        seen.append(selected)
        return

    await ContextBuilderMiddleware(profile(5000, 500)).awrap_model_call(request, handler)
    selected = seen[0]
    assert len(selected.messages) == 3
    assert selected.messages[1].tool_calls[0]["id"] == selected.messages[2].tool_call_id == "c1"
    assert "artifact_id=" in selected.messages[2].content
    assert original.content == "response-part " * 4000
    reference = selected.messages[2].additional_kwargs["source_artifact_id"]
    async with database.session() as db:
        artifact = await db.get(ArtifactRow, UUID(reference))
    assert artifact is not None
    source = json.loads(
        ArtifactStore(database.settings).resolve(artifact.relative_key).read_bytes()
    )
    assert source["messages"][0]["content"] == original.content
    assert context.toolbox.context_manifest is not None
    assert context.toolbox.context_manifest["replacements"][0]["message_id"] == "t1"


async def test_oversized_required_return_field_is_blocked_without_network(database, unique_name):
    context = await context_for(database, unique_name)
    model = ScriptedChatModel(responses=[final_text("unused")])
    request = ModelRequest(
        model=model,
        messages=[
            AIMessage(
                content="",
                id="a1",
                additional_kwargs={"reasoning_content": "hidden-sentinel" * 5000},
            )
        ],
        tools=[],
        runtime=cast(Any, SimpleNamespace(context=context)),
    )
    called = False

    async def handler(selected) -> Any:
        nonlocal called
        called = True

    with pytest.raises(ContextBudgetExceeded):
        await ContextBuilderMiddleware(profile(3000, 500)).awrap_model_call(request, handler)
    assert not called
    assert request.messages[0].additional_kwargs["reasoning_content"].startswith("hidden-sentinel")


class SummaryModel(ScriptedChatModel):
    prompts: list[str] = Field(default_factory=list)
    fail: bool = False

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append(str(messages[0].content))
        if self.fail:
            raise RuntimeError("synthetic-summary-failure")
        return self._generate(messages, stop, None, **kwargs)


async def test_compaction_chunks_visible_history_updates_structured_state_and_observes_phase(
    database, unique_name
):
    context = await context_for(database, unique_name)
    structured = json.dumps(
        {
            "current_focus": "核对条件",
            "key_findings": [{"observation": "候选观察", "artifact_id": "synthetic-ref"}],
            "failed_approaches": [{"condition": "条件未成立"}],
            "unverified_hypotheses": ["仍是假设"],
            "open_questions": ["待复查"],
            "next_steps": ["改变测试条件"],
        },
        ensure_ascii=False,
    )
    model = SummaryModel(
        responses=[
            AIMessage(
                content=structured,
                usage_metadata={"input_tokens": 123, "output_tokens": 10, "total_tokens": 133},
            )
        ]
    )
    middleware = GuardedSummarizationMiddleware(
        model=model,
        profile=profile(5000, 500),
        trigger=("tokens", 1500),
        keep=("messages", 2),
        request_timeout_seconds=2,
    )
    messages = [
        HumanMessage(content="目标", id="u1"),
        AIMessage(
            content="旧可见资料 " * 3000,
            id="a1",
            additional_kwargs={"reasoning_content": "hidden-sentinel" * 1000},
        ),
        HumanMessage(content="新反馈", id="u2"),
        AIMessage(content="新回复", id="a2"),
    ]
    state = {
        "messages": messages,
        "objective": "只测试本地",
        "user_constraints": ["不提交订单"],
        "work_summary": {"summary_version": 0, "next_steps": ["过时动作"]},
    }
    update = await middleware.abefore_model(state, SimpleNamespace(context=context))
    assert update is not None
    assert len(model.prompts) >= 2
    assert all("hidden-sentinel" not in prompt for prompt in model.prompts)
    assert update["work_summary"]["next_steps"] == ["改变测试条件"]
    assert update["work_summary"]["failed_approaches"] == [{"condition": "条件未成立"}]
    assert update["work_summary"]["unverified_hypotheses"] == ["仍是假设"]
    assert state["work_summary"]["next_steps"] == ["过时动作"]
    for prompt in model.prompts:
        estimate = estimate_request(
            [HumanMessage(content=prompt)], None, [], profile(5000, 500), model=model
        )
        assert estimate.estimated_input_tokens <= estimate.allowed_input_tokens
    async with database.session() as db:
        calls = (
            (
                await db.execute(
                    select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(calls) == len(model.prompts) and all(call.phase == "compaction" for call in calls)
    assert all(call.input_tokens == 123 for call in calls)
    ref = update["work_summary"]["source_archive_artifact_id"]
    async with database.session() as db:
        artifact = await db.get(ArtifactRow, UUID(ref))
    assert artifact is not None
    assert "hidden-sentinel" not in ArtifactStore(database.settings).resolve(
        artifact.relative_key
    ).read_text(encoding="utf-8")


async def test_summary_failure_preserves_state_and_has_no_framework_retry(database, unique_name):
    context = await context_for(database, unique_name)
    model = SummaryModel(responses=[final_text("unused")], fail=True)
    middleware = GuardedSummarizationMiddleware(
        model=model, profile=profile(5000, 500), trigger=("tokens", 1000), keep=("messages", 2)
    )
    state = {
        "messages": [
            HumanMessage(content="资料" * 5000, id="u1"),
            HumanMessage(content="最新", id="u2"),
            AIMessage(content="最新答复", id="a2"),
        ],
        "objective": "本地验收",
    }
    original = [message.model_dump() for message in state["messages"]]
    with pytest.raises(CompactionFailed):
        await middleware.abefore_model(state, SimpleNamespace(context=context))
    assert [message.model_dump() for message in state["messages"]] == original
    assert len(model.prompts) == 1
    async with database.session() as db:
        call = (
            await db.execute(
                select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
            )
        ).scalar_one()
    assert (
        call.phase == "compaction"
        and call.usage_status == "unknown"
        and call.error_code == "RuntimeError"
    )


async def test_auxiliary_sessions_and_retry_attempts_are_isolated(database, unique_name):
    first, second = await asyncio.gather(
        context_for(database, unique_name + "-1"), context_for(database, unique_name + "-2")
    )
    logical = uuid4()

    async def failure():
        raise RuntimeError("synthetic-first-attempt")

    async def success():
        await asyncio.sleep(0.01)
        return AIMessage(content="visible")

    with pytest.raises(RuntimeError):
        await invoke_attempt(
            failure,
            scope=InvocationScope(first.toolbox),
            phase="memory_distill",
            logical_id=logical,
        )
    await asyncio.gather(
        invoke_attempt(
            success,
            scope=InvocationScope(first.toolbox),
            phase="memory_distill",
            logical_id=logical,
        ),
        invoke_attempt(
            success, scope=InvocationScope(second.toolbox), phase="compaction", logical_id=uuid4()
        ),
    )
    async with database.session() as db:
        rows = (
            (
                await db.execute(
                    select(ModelCallRow).where(
                        ModelCallRow.run_id.in_([first.toolbox.run_id, second.toolbox.run_id])
                    )
                )
            )
            .scalars()
            .all()
        )
    first_rows = sorted(
        [row for row in rows if row.run_id == first.toolbox.run_id], key=lambda row: row.retry_index
    )
    assert [row.retry_index for row in first_rows] == [0, 1]
    assert len({row.model_call_id for row in rows}) == 3
    assert all(
        row.phase == "memory_distill" and row.session_id == first.toolbox.session_id
        for row in first_rows
    )


async def test_late_response_after_swallowed_timeout_is_not_returned_or_archived(
    database, unique_name
):
    context = await context_for(database, unique_name)

    async def late():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return AIMessage(content="late-sentinel")

    with pytest.raises(ModelRequestTimeout):
        await invoke_attempt(
            late,
            scope=InvocationScope(context.toolbox),
            phase="compaction",
            logical_id=uuid4(),
            request_timeout_seconds=0.02,
        )
    async with database.session() as db:
        row = (
            await db.execute(
                select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
            )
        ).scalar_one()
    assert row.error_code == "ModelRequestTimeout" and row.response_artifact_id is None


@pytest.mark.parametrize("failure_point", ["begin_attempt", "reconcile"])
async def test_accounting_failure_spools_and_syncs_without_new_model_call(
    database, unique_name, monkeypatch, failure_point
):
    context = await context_for(database, unique_name)
    calls = 0

    async def fail_repository(self, **kwargs):
        raise RuntimeError("synthetic-accounting-failure")

    async def success():
        nonlocal calls
        calls += 1
        return AIMessage(
            content="visible-result",
            usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        )

    with monkeypatch.context() as patch:
        patch.setattr(ModelCallRepository, failure_point, fail_repository)
        response = await invoke_attempt(
            success, scope=InvocationScope(context.toolbox), phase="compaction", logical_id=uuid4()
        )
    assert response.content == "visible-result" and calls == 1
    pending = read_pending_observations(database.settings.data_root, {str(context.toolbox.run_id)})
    assert (
        len(pending) == 1
        and pending[0]["input_tokens"] == 12
        and pending[0]["phase"] == "compaction"
    )
    result = await sync_model_observations(database)
    assert result == {"synced": 1, "failed": []}
    assert await sync_model_observations(database) == {"synced": 0, "failed": []}
    assert calls == 1
    async with database.session() as db:
        row = (
            await db.execute(
                select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
            )
        ).scalar_one()
    assert row.input_tokens == 12 and row.output_tokens == 3 and not row.pending_sync


class GoalSummaryModel(ScriptedChatModel):
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        record = json.loads(str(messages[0].content))
        await asyncio.sleep(0.02)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=json.dumps(
                            {
                                "current_focus": record["objective"],
                                "next_steps": [record["objective"]],
                            },
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )


async def test_shared_compaction_instance_keeps_concurrent_session_scopes_separate(
    database, unique_name
):
    first, second = await asyncio.gather(
        context_for(database, unique_name + "-a"), context_for(database, unique_name + "-b")
    )
    model = GoalSummaryModel()
    middleware = GuardedSummarizationMiddleware(
        model=model, trigger=("messages", 3), keep=("messages", 2), summary_prompt="{messages}"
    )
    states = [
        {
            "messages": [
                HumanMessage(content=f"{goal}-{index}", id=f"{goal}-{index}") for index in range(5)
            ],
            "objective": goal,
        }
        for goal in ("目标甲", "目标乙")
    ]
    updates = await asyncio.gather(
        middleware.abefore_model(states[0], SimpleNamespace(context=first)),
        middleware.abefore_model(states[1], SimpleNamespace(context=second)),
    )
    for update, context, state in zip(updates, (first, second), states, strict=True):
        assert update is not None
        assert update["work_summary"]["current_focus"] == state["objective"]
        async with database.session() as db:
            artifact = await db.get(
                ArtifactRow, UUID(update["work_summary"]["source_archive_artifact_id"])
            )
            call = (
                await db.execute(
                    select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
                )
            ).scalar_one()
        assert artifact is not None and artifact.session_id == context.toolbox.session_id
        assert call.session_id == context.toolbox.session_id and call.phase == "compaction"
