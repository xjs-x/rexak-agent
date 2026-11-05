"""Native Main + Pi + A2A + Fake Executor, no provider or target network calls."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.runtime import Runtime
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.collaboration import CollaborationMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.collaboration import DelegatedMessageInput, WorkerFindingRequest
from agent_workbench.contracts.delegation import DelegationInput
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.domain.collaboration import CollaborationService, collaboration_signature
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    DelegationCollaborationRow,
    RunRow,
)
from agent_workbench.tools.delegation import DELEGATION_TOOLS
from agent_workbench.tools.runtime import ResourceLocks, Toolbox
from tests.integration.test_ultra_delegations import TOKEN, make_manager
from tests.integration.test_worker_evidence import artifact, evidence_case, free_port  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("change_target", [False, True], ids=["changed-message", "changed-worker"])
async def test_parent_message_call_cannot_change_payload_or_worker(database, evidence_case, monkeypatch, change_target):  # noqa: F811
    manager, _, _, first_id, cfg = evidence_case
    second_id = uuid4()
    async with database.transaction() as db:
        row = await db.get(AgentDelegationRow, first_id)
        assert row
        row.state, row.a2a_task_id, row.worker_id = "working", str(uuid4()), str(uuid4())
        row.handoff_snapshot = {**row.handoff_snapshot, "collaboration_access": "collaboration_v1"}
        db.add(AgentDelegationRow(id=second_id, parent_session_id=manager.session_id, parent_run_id=manager.run_id,
            parent_tool_call_id="other-worker", request_digest="1" * 64, context_id=uuid4(),
            handoff_snapshot=row.handoff_snapshot, objective="synthetic", context_refs=[],
            authorization_snapshot=row.authorization_snapshot, state="working", summary="", cancel_requested=False,
            a2a_task_id=str(uuid4()), worker_id=str(uuid4())))

    async def no_delivery(*_args):
        return None

    monkeypatch.setattr(CollaborationService, "reconcile_messages", no_delivery)
    service = CollaborationService(database, cfg)
    value = DelegatedMessageInput(message="original")
    first = await service.send(manager.session_id, manager.run_id, first_id, "same-parent-call", value)
    assert first == await service.send(manager.session_id, manager.run_id, first_id, "same-parent-call", value)
    with pytest.raises(ApiError) as error:
        await service.send(manager.session_id, manager.run_id, second_id if change_target else first_id,
            "same-parent-call", value if change_target else DelegatedMessageInput(message="changed"))
    assert error.value.code == ErrorCode.IDEMPOTENCY_KEY_REUSED
    async with database.session() as db:
        assert len((await db.scalars(select(DelegationCollaborationRow).where(
            DelegationCollaborationRow.parent_run_id == manager.run_id))).all()) == 1


@pytest.fixture
async def collaboration_runtime(database, settings, tmp_path):
    api_port, worker_port = free_port(), free_port()
    cfg = settings.model_copy(update={"ultra_worker_url": f"http://127.0.0.1:{worker_port}",
        "ultra_worker_token": TOKEN, "ultra_worker_execution_enabled": True})
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME"}}
    env.update(WORKBENCH_DATABASE_URL=settings.database_url, WORKBENCH_DATA_ROOT=str(settings.data_root),
        WORKBENCH_ULTRA_WORKER_URL=cfg.ultra_worker_url, WORKBENCH_ULTRA_WORKER_TOKEN=TOKEN,
        WORKBENCH_ULTRA_WORKER_EXECUTION_ENABLED="true", WORKBENCH_OWNER_ID=str(settings.owner_id),
        WORKBENCH_API_PORT=str(api_port), PI_FAKE_ROOT=str(tmp_path / "kali"),
        REXAK_WORKER_DATABASE_URL=make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False),
        REXAK_WORKER_TOKEN=TOKEN, FIXTURE_PORT=str(worker_port), REXAK_WORKER_CONTROL_URL=f"http://127.0.0.1:{api_port}",
        PYTHONIOENCODING="utf-8")
    if os.environ.get("PI_REAL_EXECUTOR") == "true":
        env.update({k: v for k, v in os.environ.items() if k.startswith("WORKBENCH_EXECUTOR_")})
        env["PI_REAL_EXECUTOR"] = "true"
    node = shutil.which("node")
    assert node
    with (tmp_path / "api.log").open("wb") as api_log, (tmp_path / "worker.log").open("wb") as worker_log:
        api = subprocess.Popen([sys.executable, "-m", "tests.support.collaboration_api"], env=env, cwd=REPO_ROOT, stdout=api_log, stderr=api_log)
        worker = subprocess.Popen([node, "--import", "tsx", "test/collaboration-a2a-server.ts"], env=env, cwd=REPO_ROOT / "worker", stdout=worker_log, stderr=worker_log)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                for url in [f"http://127.0.0.1:{api_port}/health", cfg.ultra_worker_url + "/health"]:
                    for _ in range(200):
                        assert api.poll() is None and worker.poll() is None
                        try:
                            if (await client.get(url, headers={"Authorization": "Bearer " + TOKEN})).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        raise AssertionError("Collaboration runtime startup timeout")
            yield cfg
        finally:
            for process in (worker, api):
                process.terminate()
                process.wait(timeout=10)


def toolbox(database, cfg, manager):
    async def check(*_args):
        return None

    return Toolbox(db=database, settings=cfg, session_id=manager.session_id,
        run_id=manager.run_id, owner="test-owner", owner_generation=1, control_version=0,
        scope=ScopePolicy(ScopeSnapshot(mode="instruction")), executor=cast(Any, None),
        artifacts=ArtifactStore(cfg), spans=SpanRecorder(), locks=ResourceLocks(cfg), control_check=check)


async def test_native_main_and_pi_use_live_messages_before_worker_completion(database, collaboration_runtime, monkeypatch):
    cfg = collaboration_runtime
    manager = await make_manager(database, cfg)
    history = await artifact(database, cfg, manager.session_id, b"stage4 historical progress", "stage4-history")
    requests = []
    original = A2AWorkerClient.send_context

    async def lost_once(self, request_id, message_id, payload):
        result = await original(self, request_id, message_id, payload)
        requests.append(message_id)
        if len(requests) == 1:
            raise httpx.ReadTimeout("response lost after enqueue")
        return result

    monkeypatch.setattr(A2AWorkerClient, "send_context", lost_once)

    class MainModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "stage4-controlled-main"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            outputs = [m for m in messages if isinstance(m, ToolMessage)]
            hints = [m for m in messages if str(m.id).startswith("worker-finding:")]
            if not outputs:
                name, args = "delegate_task", {"objective": "Inspect synthetic history and share important hints", "context_refs": [str(history)]}
            else:
                first = json.loads(str(outputs[0].content))
                if len(outputs) == 1:
                    name, args = "wait_delegated_task", {"delegation_id": first["id"], "seconds": 10}
                elif len(outputs) == 2:
                    assert len(hints) == 1 and "stage4-important-hint" in str(hints[0].content)
                    assert json.loads(str(outputs[-1].content))["state"] not in {"completed", "failed"}
                    name, args = "send_delegated_message", {"delegation_id": first["id"], "message": "stage4-main-clue", "context_refs": [str(history)]}
                elif len(outputs) == 3:
                    assert len(hints) == 1
                    name, args = "wait_delegated_task", {"delegation_id": first["id"], "seconds": 10}
                elif len(outputs) == 4:
                    name, args = "get_delegated_task", {"delegation_id": first["id"]}
                else:
                    last = json.loads(str(outputs[-1].content))
                    assert last["state"] == "completed", last
                    assert "Worker used 1 supplements; clue=stage4-main-clue" in last["summary"]
                    assert last["message_receipts"][0]["status"] == "consumed"
                    assert len(hints) == 1
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Main used live finding and Worker used the new clue"))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                "id": f"main-{len(outputs)}", "name": name, "args": args}]))])

    saver = InMemorySaver()
    graph = create_agent(MainModel(), tools=DELEGATION_TOOLS, state_schema=AgentGraphState,
        context_schema=RunContext, middleware=[CollaborationMiddleware()], checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(manager.session_id)}}
    context = RunContext(toolbox=toolbox(database, cfg, manager))
    result = await graph.ainvoke({"messages": [HumanMessage(content="Collaborate on synthetic data")]}, config, context=context)
    assert result["messages"][-1].content == "Main used live finding and Worker used the new clue"
    assert len(requests) == 1  # Lost response reconciled by GET, no second enqueue.
    assert result["collaboration_cursor"]["sequence"] == 1
    async with database.session() as db:
        event = await db.scalar(select(DelegationCollaborationRow).where(
            DelegationCollaborationRow.parent_run_id == manager.run_id, DelegationCollaborationRow.direction == "finding"))
        assert event.payload["verification"] == "unverified_candidate"
        assert event.payload["evidence_reads"] and event.payload["output_refs"]
        count = await db.scalar(text("SELECT count(*) FROM ultra_worker.messages WHERE request_id=:id"), {"id": event.delegation_id})
        assert count == 1
    # Checkpoint survives a new middleware/context instance without redelivery.
    snapshot = await graph.aget_state(config)
    assert await CollaborationMiddleware().abefore_model(cast(AgentGraphState, snapshot.values), Runtime(context=context)) is None


async def test_concurrent_workers_order_wait_wake_and_candidate_no_evidence(database, collaboration_runtime):
    cfg = collaboration_runtime
    manager = await make_manager(database, cfg)
    history = await artifact(database, cfg, manager.session_id, b"multi-worker history")
    records = await asyncio.gather(*(manager.delegate(f"parallel-{i}", DelegationInput(objective="Analyze", context_refs=[history]),
        owner="test-owner", generation=1, control_version=0) for i in range(2)))
    service = CollaborationService(database, cfg)
    first = await manager.wait(records[0].id, 10, findings_after=0)
    assert first.state in {"accepted", "working"}, first.model_dump_json()
    events = []
    for _ in range(80):
        events = await service.findings(manager.session_id, manager.run_id)
        if len(events) == 2:
            break
        await asyncio.sleep(0.05)
    assert len(events) == 2 and [e["sequence"] for e in events] == [1, 2]
    assert {e["delegation_id"] for e in events} == {str(r.id) for r in records}
    assert all(e["output_refs"] and e["evidence_reads"] for e in events)
    other = await make_manager(database, cfg)
    with pytest.raises(ApiError):
        await service.send(other.session_id, other.run_id, records[0].id, "bad-owner", DelegatedMessageInput(message="no"))
    row = await manager.refresh(records[0].id)
    assert row.a2a_task_id and row.worker_id and row.context_id
    value = WorkerFindingRequest(task_id=row.a2a_task_id, worker_id=UUID(row.worker_id), context_id=row.context_id,
        tool_call_id="candidate-without-refs", summary="Unverified hypothesis")
    signature = collaboration_signature(TOKEN, row.id, value)
    assert await service.report(row.id, value, signature) == await service.report(row.id, value, signature)
    with pytest.raises(ApiError):
        changed = value.model_copy(update={"summary": "conflicting summary"})
        await service.report(row.id, changed, signature)
    with pytest.raises(ApiError):
        await service.report(row.id, value, "wrong-token")
    await manager.cancel(records[0].id)
    with pytest.raises(ApiError):
        await service.report(row.id, value.model_copy(update={"tool_call_id": "after-cancel"}), signature)
    with pytest.raises(ApiError):
        await service.send(manager.session_id, manager.run_id, row.id, "late-message", DelegatedMessageInput(message="no"))
    sibling = await manager.wait(records[1].id, 10)
    assert sibling.state == "completed"


async def test_delivery_checkpoint_replay_pause_and_source_rejection(database, evidence_case):  # noqa: F811
    manager, _, _, delegation_id, cfg = evidence_case
    async with database.transaction() as db:
        row = await db.get(AgentDelegationRow, delegation_id)
        row.handoff_snapshot = {**row.handoff_snapshot, "collaboration_access": "collaboration_v1"}
        row.authorization_snapshot = {**row.authorization_snapshot, "tool_allowlist": ["report_finding"]}
    service = CollaborationService(database, cfg)
    # Fixed remote identity comes from the evidence_case transport fixture.
    async with A2AWorkerClient(cfg) as client:
        remote = await client.lookup(delegation_id)
    assert remote
    value = WorkerFindingRequest(task_id=remote["task_id"], worker_id=remote["worker_id"],
        context_id=remote["task"]["contextId"], tool_call_id="report", summary="checkpoint-replay-marker")
    signature = collaboration_signature(TOKEN, delegation_id, value)
    await service.report(delegation_id, value, signature)
    context = RunContext(toolbox=toolbox(database, cfg, manager))
    middleware = CollaborationMiddleware()
    # Crash before checkpoint commitment: same IDs, no DB consumed flag.
    first = await middleware.abefore_model({"messages": []}, Runtime(context=context))
    replay = await middleware.abefore_model({"messages": []}, Runtime(context=context))
    assert first is not None
    assert first == replay and first["messages"][0].id.startswith("worker-finding:")
    assert await middleware.abefore_model(cast(AgentGraphState, first), Runtime(context=context)) is None
    # PostgreSQL checkpoint survives a closed controller connection and model failure.
    observed = []

    class FailingOnceMain(BaseChatModel):
        @property
        def _llm_type(self):
            return "checkpoint-delivery-test"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            ids = [m.id for m in messages if str(m.id).startswith("worker-finding:")]
            observed.append(ids)
            if len(observed) == 1:
                raise RuntimeError("synthetic model failure after checkpoint")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Recovered candidate"))])

    config: RunnableConfig = {"configurable": {"thread_id": "stage4-restart-" + str(manager.session_id)}}
    async with open_checkpointer_connection(cfg) as conn:
        graph = create_agent(FailingOnceMain(), state_schema=AgentGraphState, context_schema=RunContext,
            middleware=[CollaborationMiddleware()], checkpointer=AsyncPostgresSaver(conn))
        with pytest.raises(RuntimeError, match="synthetic model failure"):
            await graph.ainvoke({"messages": [HumanMessage(content="Receive candidate")]}, config, context=context)
    async with open_checkpointer_connection(cfg) as conn:
        rebuilt = create_agent(FailingOnceMain(), state_schema=AgentGraphState, context_schema=RunContext,
            middleware=[CollaborationMiddleware()], checkpointer=AsyncPostgresSaver(conn))
        recovered = await rebuilt.ainvoke(None, config, context=context)
    assert recovered["messages"][-1].content == "Recovered candidate"
    assert len(observed) == 2 and observed[0] == observed[1] and len(observed[0]) == 1
    foreign = {**value.model_dump(), "tool_call_id": "bad-source", "evidence_reads": [{
        "receipt_id": str(uuid4()), "artifact_id": str(uuid4()), "sha256": "0" * 64,
        "view_sha256": "0" * 64, "offset": 0, "returned_bytes": 10, "truncated": False}]}
    with pytest.raises(ApiError):
        # Validate reconstructed input, not model_copy's unchecked nested values.
        await service.report(delegation_id, WorkerFindingRequest.model_validate(foreign), signature)
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        run.status = "paused"
    assert await middleware.abefore_model({"messages": []}, Runtime(context=context)) is None
    with pytest.raises(ApiError):
        await service.report(delegation_id, value, signature)
    async with database.session() as db:
        assert len((await db.scalars(select(DelegationCollaborationRow).where(DelegationCollaborationRow.delegation_id == delegation_id))).all()) == 1
