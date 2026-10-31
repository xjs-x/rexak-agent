"""验收真实包装链、台账、领域快照与迁移边界，网络使用本地替身。"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain.agents.middleware import ModelResponse
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr
from sqlalchemy import select

from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.agent.middleware.response_recovery import ResponseRecoveryMiddleware
from agent_workbench.agent.middleware.stage_report import StageReportMiddleware
from agent_workbench.agent.state import AgentGraphState, StateSchemaIncompatibleError
from agent_workbench.agent.state_migration import ensure_current_checkpoint
from agent_workbench.agent.working_context import assemble_working_context
from agent_workbench.api.app import create_app
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import JobState
from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
from agent_workbench.contracts.tool import JobSnapshot, ToolError
from agent_workbench.domain.leads import LeadService
from agent_workbench.domain.reports import create_report_snapshot
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.models.invocation import InvocationScope, invoke_chat_model
from agent_workbench.models.provider import ModelBinding
from agent_workbench.models.response_quality import ModelResponseIncomplete
from agent_workbench.models.xiaomi import XiaomiChatOpenAI
from agent_workbench.persistence.repositories import ToolOperationRepository
from agent_workbench.persistence.tables import ModelCallRow, ReportVersionRow, ToolOperationRow
from agent_workbench.tools.http import HttpRequestArgs, run_http_request
from agent_workbench.tools.task import _evidence_operation_context, _link_asset
from tests.integration.test_request_budget import context_for
from tests.support.scripted_model import ScriptedChatModel
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration

async def _operation(database, context, *, digest: str, tool_call_id: str, status: str):
    async with database.transaction() as db:
        row, _created = await ToolOperationRepository(db).prepare(
            operation_id=uuid4(),
            run_id=context.toolbox.run_id,
            session_id=context.toolbox.session_id,
            tool_call_id=tool_call_id,
            tool_name="http_request",
            request_digest=digest,
            effect_class="read_only",
            policy_revision=1,
            scope_revision=1,
            dispatch_control_version=1,
        )
        row.status = status
    return row


async def test_actual_wire_recovery_limit_matches_manifest_and_each_attempt(database, unique_name):
    context = await context_for(database, unique_name)
    captured = []

    def provider(request: httpx.Request):
        captured.append(json.loads(request.content))
        truncated = len(captured) == 1
        return httpx.Response(
            200,
            json={
                "id": f"synthetic-{len(captured)}",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "" if truncated else "done",
                        },
                        "finish_reason": "length" if truncated else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )

    selected_profile = profile(50000, 1000).model_copy(update={"recovery_output_limit": 16000})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = XiaomiChatOpenAI(
            model="fixture",
            api_key=SecretStr("synthetic"),
            base_url="http://fixture.invalid/v1",
            max_completion_tokens=1000,
            max_retries=0,
            use_responses_api=False,
            http_async_client=client,
        )
        wrappers = [
            item
            for item in build_agent_middleware(
                model,
                ModelBinding(selected_profile),
                get_settings(),
                [],
                default_price_catalog(),
            )
            if isinstance(
                item,
                (ResponseRecoveryMiddleware, ContextBuilderMiddleware, ModelObserverMiddleware),
            )
        ]
        assert [type(item) for item in wrappers] == [
            ResponseRecoveryMiddleware,
            ContextBuilderMiddleware,
            ModelObserverMiddleware,
        ]
        graph = create_agent(
            model=model,
            tools=[],
            state_schema=AgentGraphState,
            context_schema=type(context),
            middleware=wrappers,
        )
        result = await graph.ainvoke(
            cast(Any, {"messages": [HumanMessage(content="synthetic", id="stable-user")]}),
            context=context,
        )
    assert result["messages"][-1].content == "done"
    assert [wire.get("max_completion_tokens", wire.get("max_tokens")) for wire in captured] == [
        1000,
        16000,
    ]
    manifest = context.toolbox.context_manifest
    assert manifest is not None
    budget = manifest["budget"]
    assert budget["output_limit"] == 16000 and budget["allowed_input_tokens"] == 30600
    async with database.session() as db:
        calls = list(
            (
                await db.execute(
                    select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
                )
            ).scalars()
        )
    assert len(calls) == 2 and len({call.logical_call_id for call in calls}) == 1
    assert all(call.input_tokens == 10 and call.output_tokens == 3 for call in calls)


async def test_incomplete_visible_reply_is_labeled_in_conversation_archive(database, unique_name):
    context = await context_for(database, unique_name)
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="synthetic partial", response_metadata={"finish_reason": "length"}),
            AIMessage(content="synthetic accepted"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[],
        state_schema=AgentGraphState,
        context_schema=type(context),
        middleware=[
            ResponseRecoveryMiddleware(),
            ContextBuilderMiddleware(profile()),
            ModelObserverMiddleware(),
        ],
    )
    await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="synthetic", id="u")]}), context=context
    )
    with TestClient(create_app()) as client:
        assert client.post("/api/session").status_code == 200
        page = client.get(f"/api/sessions/{context.toolbox.session_id}/conversation")
        assert page.status_code == 200
        entries = {entry["preview"]: entry for entry in page.json()["entries"]}
        assert entries["synthetic partial"]["response_outcome"] == "truncated"
        assert entries["synthetic accepted"]["response_outcome"] == "usable"


async def test_recovery_does_not_send_when_quota_read_fails(database, unique_name):
    context = await context_for(database, unique_name)
    sent = []

    def fail_session():
        raise RuntimeError("synthetic database unavailable")

    cast(Any, context.toolbox).db = SimpleNamespace(session=fail_session)
    request = ModelRequest(
        model=ScriptedChatModel(responses=[]),
        messages=[HumanMessage(content="test", id="u")],
        tools=[],
        runtime=cast(Any, SimpleNamespace(context=context)),
        state=cast(Any, {}),
    )

    async def handler(_request):
        sent.append(1)
        return ModelResponse(result=[AIMessage(content="unused")])

    with pytest.raises(RuntimeError, match="database unavailable"):
        await ResponseRecoveryMiddleware().awrap_model_call(request, handler)
    assert sent == []


@pytest.mark.parametrize("phase", ["compaction", "memory_distillation"])
async def test_incomplete_auxiliary_response_is_accounted_but_never_committed(
    database, unique_name, phase
):
    context = await context_for(database, unique_name)
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="plausible but incomplete", response_metadata={"finish_reason": "length"}
            )
        ]
    )
    with pytest.raises(ModelResponseIncomplete):
        await invoke_chat_model(
            model,
            [HumanMessage(content="synthetic")],
            scope=InvocationScope(context.toolbox),
            phase=phase,
            logical_id=uuid4(),
        )
    async with database.session() as db:
        calls = list(
            (
                await db.execute(
                    select(ModelCallRow).where(ModelCallRow.run_id == context.toolbox.run_id)
                )
            ).scalars()
        )
    assert len(calls) == 1 and calls[0].phase == phase


async def test_schema_two_pending_tool_results_are_not_rewritten():
    snapshot = SimpleNamespace(
        values={"state_schema_version": 2},
        parent_config={},
        tasks=[SimpleNamespace(result="completed pending write")],
    )
    with pytest.raises(StateSchemaIncompatibleError, match="并行结果"):
        await ensure_current_checkpoint(None, {}, snapshot, None, None)


async def test_first_assessment_generates_report_without_compaction_and_reuses_snapshot(
    database, unique_name
):
    context = await context_for(database, unique_name)
    state = {
        "current_focus": {"step_id": "s", "intent": "synthetic", "source": "explicit"},
        "work_assessment": {
            "step_id": "s",
            "changed": "condition established",
            "next_action": "discriminate",
            "revision": 1,
        },
    }
    middleware = StageReportMiddleware(enabled=True)
    update = await middleware.abefore_model(
        cast(Any, state), cast(Any, SimpleNamespace(context=context))
    )
    assert update and update["report_retry_key"] is None
    async with database.session() as db:
        row = (
            await db.execute(
                select(ReportVersionRow).where(
                    ReportVersionRow.session_id == context.toolbox.session_id
                )
            )
        ).scalar_one()
        stage = row.captured_snapshot["body"]["stage"]
        assert stage["work_assessment"]["changed"] == "condition established"
        assert not row.captured_snapshot["body"]["stage_coverage"]["newer_domain_records_present"]
    await create_report_snapshot(
        database, ArtifactStore(database.settings), context.toolbox.session_id, stage_metadata=stage
    )
    state.update(update)
    assert (
        await middleware.abefore_model(
            cast(Any, state), cast(Any, SimpleNamespace(context=context))
        )
        is None
    )


async def test_changed_identity_recalls_related_old_lead(database, unique_name):
    context = await context_for(database, unique_name)
    identity = str(uuid4())
    async with database.transaction() as db:
        unrelated = await LeadService(db).upsert(
            session_id=context.toolbox.session_id, hypothesis="unrelated"
        )
        related = await LeadService(db).upsert(
            session_id=context.toolbox.session_id,
            hypothesis="old conditional",
            identity_refs=[identity],
        )
    state = {"active_lead_ids": [str(unrelated.id)], "active_identity_id": identity}
    async with database.session() as db:
        view = await assemble_working_context(db, context.toolbox.session_id, state)
    selected = view["judgment"]["leads"]
    assert any(
        item["lead_id"] == str(related.id) and item["recall_reason"] == "asset_or_identity_related"
        for item in selected
    )


@pytest.mark.parametrize(
    "code,category,delivery,certainty",
    [
        ("request_construction_error", "construction", "not_applied", "not_applied"),
        ("http_timeout", "transport", "unknown", "uncertain"),
        ("http_connection_error", "transport", "unknown", "uncertain"),
    ],
)
async def test_executor_failure_is_not_reported_as_target_response(
    database, unique_name, code, category, delivery, certainty
):
    context = await context_for(database, unique_name)
    toolbox = cast(Any, context.toolbox)
    job_id = uuid4()
    operation_id = None

    async def submit(operation):
        nonlocal operation_id
        operation_id = operation["operation_id"]
        return SimpleNamespace(job_id=job_id, accepted_at=datetime.now(UTC))

    async def terminal(_job):
        return JobSnapshot(
            job_id=job_id,
            operation_id=UUID(str(operation_id)),
            kind="http_request",
            state=JobState.FAILED,
            error=ToolError(code=code, message="synthetic failure"),
        )

    async def check():
        return None

    @asynccontextmanager
    async def acquire(*_args, **_kwargs):
        yield

    toolbox.settings = database.settings
    toolbox.executor = SimpleNamespace(submit=submit)
    toolbox.wait_for_terminal = terminal
    toolbox.assert_control_current = check
    toolbox.locks = SimpleNamespace(acquire=acquire)
    toolbox.scope = ScopePolicy(
        ScopeSnapshot(rules=[ScopeRule(scheme="http", host="fixture.invalid", port=80)])
    )
    toolbox.scope_revision = toolbox.policy_revision = toolbox.control_version = (
        toolbox.owner_generation
    ) = 1
    toolbox.identity_id = None
    toolbox.interactive_threshold_seconds = 2
    result = await run_http_request(
        HttpRequestArgs(url="http://fixture.invalid/", save_body_artifact=False),
        toolbox,
        "synthetic-http",
    )
    assert result.data["error_category"] == category
    assert result.data["delivery_state"] == delivery
    assert str(result.execution_certainty) == certainty


async def test_captured_identity_and_method_override_model_labels(database, unique_name):
    context = await context_for(database, unique_name)
    source = await _operation(
        database, context, digest="a" * 64, tool_call_id="source", status="succeeded"
    )
    identity = uuid4()
    async with database.transaction() as db:
        row = await db.get(ToolOperationRow, source.operation_id)
        assert row is not None
        row.execution_context = {
            "method": "POST",
            "url": "http://fixture.invalid/",
            "identity": "anonymous",
        }
        # 这里只验证解析来源；真实身份行由既有身份工具负责。
        captured = SimpleNamespace(
            operation_id=row.operation_id,
            session_id=row.session_id,
            execution_context=row.execution_context,
            identity_id=identity,
        )

        class SourceReader:
            async def get(self, model, key):
                return captured

        evidence = await _evidence_operation_context(
            SourceReader(), context.toolbox, [], source_operation_id=str(row.operation_id)
        )
        assert evidence["identity_refs"] == [str(identity)]
        with pytest.raises(ValueError, match="真实采集"):
            await _link_asset(
                db,
                context.toolbox,
                url="http://fixture.invalid/",
                explicit_method="GET",
                identity_refs=[],
                evidence_context=evidence,
                notes="synthetic",
            )
