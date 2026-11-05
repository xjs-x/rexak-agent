"""Five-P1 repair probes reuse original Runner, collaboration and Executor fixtures."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.collaboration import DelegatedMessageInput
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.agent_delegations import reconcile_inactive_parents
from agent_workbench.domain.collaboration import CollaborationService
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.execution.client import ExecutorConnectionError
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    DelegationCollaborationRow,
    EventRow,
    RunRow,
    SessionRow,
)
from agent_workbench.runtime.runner import AgentRunner
from tests.integration.test_natural_completion_safety import runtime_for
from tests.integration.test_pi_worker_tools import make_case
from tests.integration.test_worker_evidence import evidence_case  # noqa: F401
from tests.support.pi_executor import PiFakeExecutor

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("confirmed", [True, False])
async def test_final_end_persists_stop_before_wait_and_existing_reconciler_cancels_only_this_run(database, settings, monkeypatch, tmp_path, confirmed):
    fake = PiFakeExecutor(tmp_path)
    fake.hang = True
    manager, ident, call = await make_case(database, settings, monkeypatch, fake)
    _other, other_id, other_call = await make_case(database, settings, monkeypatch, fake)
    await call("bash", "begin", {"command": "sleep"})
    pending = await call("bash", "step", {"action": "bash", "command": "sleep"}, step=1)
    await other_call("bash", "begin", {"command": "sleep"})
    other_pending = await other_call("bash", "step", {"action": "bash", "command": "sleep"}, step=1)
    async with database.session() as db:
        run, task = await db.get(RunRow, manager.run_id), await db.get(SessionRow, manager.session_id)
    runner = AgentRunner(database=database, settings=settings, owner="test-owner")
    # Ordinary tool waits / segment yields never cancel anything.
    assert await runner._resolve_after_segment(runtime_for(run, segment_yield_reason="tools_pending"), run, task, SpanRecorder()) == (None, None)
    async with database.session() as db:
        assert not (await db.get(AgentDelegationRow, ident)).cancel_requested
    # Main emits final text without calling cancel. END is replay-safe and cannot wait for the slow leaf first.
    graph = create_agent(FakeListChatModel(responses=["Synthetic goal result; redundant exploration is unnecessary"]),
        state_schema=AgentGraphState, checkpointer=InMemorySaver())
    await graph.ainvoke(cast(Any, {"messages": [HumanMessage(content="Return the achieved synthetic result")],
        "active_run_id": str(run.id), "dispatch_control_version": run.control_version}),
        {"configurable": {"thread_id": task.thread_id}})
    runtime = cast(Any, SimpleNamespace(graph=graph))
    status = None
    for _ in range(2):
        status, _ = await runner._resolve_after_segment(runtime, run, task, SpanRecorder())
        assert status in {RunStatus.WAITING_TOOL, RunStatus.RECOVERING}
    async with database.session() as db:
        row = await db.get(AgentDelegationRow, ident)
        assert row.cancel_requested and row.state == "cancel_requested"
        assert fake.resources[pending["resource_id"]].state == "running"
        assert not (await db.get(AgentDelegationRow, other_id)).cancel_requested
        assert await db.scalar(select(func.count()).select_from(EventRow).where(EventRow.run_id == run.id, EventRow.payload["event"].astext == "final_response_saved")) == 1
        saved = await db.get(RunRow, run.id)
        assert saved.config_snapshot["completion_result_artifact_id"]
    assert status is not None
    async with database.transaction() as db:
        saved = await db.get(RunRow, run.id)
        saved.status = str(status)

    @asynccontextmanager
    async def executor(_settings):
        yield fake

    async def remote_cancel(_self, _id, _task_id):
        return None  # Controlled A2A stop receipt; real leaf cancellation remains the original settle_worker path.

    monkeypatch.setattr("agent_workbench.domain.worker_tools.open_executor", executor)
    monkeypatch.setattr(A2AWorkerClient, "cancel", remote_cancel)
    if not confirmed:
        submit = fake.execution_submit

        async def lose_cancel(envelope):
            if envelope["action"] == "cancel_resource":
                raise ExecutorConnectionError("cancellation response and resource exit unconfirmed")
            return await submit(envelope)

        monkeypatch.setattr(fake, "execution_submit", lose_cancel)
    await reconcile_inactive_parents(database, settings, only_run_id=manager.run_id)
    async with database.session() as db:
        stopped = await db.get(AgentDelegationRow, ident)
        assert stopped.state == ("cancelled" if confirmed else "cancel_requested")
    assert fake.resources[pending["resource_id"]].state == ("exited" if confirmed else "running")
    assert fake.resources[other_pending["resource_id"]].state == "running"


async def test_unknown_registered_message_blocks_successor_until_reconciled(database, evidence_case, monkeypatch):  # noqa: F811
    manager, _, _, ident, cfg = evidence_case
    async with database.transaction() as db:
        row = await db.get(AgentDelegationRow, ident)
        row.state, row.a2a_task_id, row.worker_id = "working", str(uuid4()), str(uuid4())
        row.handoff_snapshot = {**row.handoff_snapshot, "collaboration_access": "collaboration_v1"}
    original = CollaborationService.reconcile_messages

    async def hold(*_args):
        pass

    monkeypatch.setattr(CollaborationService, "reconcile_messages", hold)
    service = CollaborationService(database, cfg)
    first = await service.send(manager.session_id, manager.run_id, ident, "first", DelegatedMessageInput(message="clue 1"))
    second = await service.send(manager.session_id, manager.run_id, ident, "second", DelegatedMessageInput(message="clue 2"))
    first_id, second_id = UUID(first["message_id"]), UUID(second["message_id"])
    registered, released, consumed = False, False, False
    posts = []

    async def receipt(_self, _delegation, message):
        if message == first_id and registered:
            return {"id": str(message), "status": "consumed" if consumed else "queued" if released else "delivery_unknown"}
        if message == second_id and message in posts:
            return {"id": str(message), "status": "consumed" if consumed else "queued"}
        return None

    async def send(_self, _delegation, message, _payload):
        nonlocal registered
        posts.append(message)
        if message == first_id:
            registered = True
            raise httpx.ReadTimeout("server registered first message; admission delayed")
        return {"id": str(message), "status": "queued"}

    monkeypatch.setattr(A2AWorkerClient, "message_status", receipt)
    monkeypatch.setattr(A2AWorkerClient, "send_context", send)
    for _ in range(2):
        await original(service, manager.session_id, manager.run_id, ident)
        assert posts == [first_id]  # Second cannot overtake a valid delivery_unknown receipt.
    async with database.session() as db:
        assert (await db.get(DelegationCollaborationRow, second_id)).delivery_status == "accepted"
    released = True
    await original(service, manager.session_id, manager.run_id, ident)
    assert posts == [first_id, second_id]
    consumed = True
    await original(service, manager.session_id, manager.run_id, ident)
    assert posts == [first_id, second_id]
    async with database.session() as db:
        rows = (await db.scalars(select(DelegationCollaborationRow).where(DelegationCollaborationRow.id.in_([first_id, second_id])))).all()
        assert [r.delivery_status for r in rows] == ["consumed", "consumed"]
