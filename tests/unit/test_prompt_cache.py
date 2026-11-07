"""Verify immutable request prefixes across official checkpoints and state changes."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.context_updates import append_update
from agent_workbench.agent.middleware.history_assistance import HistoryAssistanceMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.delegation import WorkerUsage
from agent_workbench.contracts.usage import AgentTokenSummary
from agent_workbench.domain.worker_usage import add_worker_usage
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import final_text
from tests.unit.test_history_assistance_mvp import Request, board_for, box_for, paired


async def test_context_changes_append_and_restore_without_rewriting_prefix(tmp_path):
    model = SteppedModel(steps=[lambda _: final_text("answer")])
    saver = InMemorySaver()
    environment = {"executor_id": "fixture", "observed_at": "2026-11-07T00:00:00Z",
                   "tools": {"python": {"available": True}}, "missing": []}
    box = SimpleNamespace(settings=Settings(data_root=tmp_path, history_assistance_mode="off"),
                          working_context={"pending_job_ids": [], "marker": "A",
                                           "execution_environment": environment}, scope=None)
    context = RunContext(toolbox=cast(Any, box), thread_id="cache-test")

    def graph():
        return create_agent(model=model, tools=[], system_prompt="fixed system",
                            middleware=[ContextBuilderMiddleware()], state_schema=AgentGraphState,
                            context_schema=RunContext, checkpointer=saver)

    config: RunnableConfig = {"configurable": {"thread_id": "cache-test"}}
    agent = graph()
    first = await agent.ainvoke(cast(Any, {"messages": [HumanMessage(content="task", id="u1")],
        "objective": "task", "current_focus": {"intent": "old focus"}}), config, context=context)
    old_input = deepcopy(model.received[0])
    first_update = next(m for m in first["messages"] if m.additional_kwargs.get("host_projection"))
    assert first_update == model.received[0][-1]

    box.working_context = {"pending_job_ids": [], "marker": "B", "execution_environment": environment}
    agent = graph()  # A new runtime must reuse the checkpoint, not in-process history.
    second = await agent.ainvoke(cast(Any, {"messages": [HumanMessage(content="continue", id="u2")],
        "current_focus": None}), config, context=context)
    assert model.received[1][:len(old_input)] == old_input
    updates = [m for m in second["messages"] if m.additional_kwargs.get("host_projection")]
    assert len(updates) == 2 and updates[0] == first_update
    assert '"marker": "B"' in str(updates[-1].content)
    assert "old focus" not in str(updates[-1].content)
    assert environment["observed_at"] in str(updates[-1].content)
    assert model.received[0][0].content == model.received[1][0].content
    assert "old focus" not in str(model.received[0][0].content)

    third = await agent.ainvoke(cast(Any, {"messages": [HumanMessage(content="continue", id="u3")]}),
                               config, context=context)
    assert len([m for m in third["messages"] if m.additional_kwargs.get("host_projection")]) == 2


async def test_failed_call_does_not_modify_checkpoint_history(tmp_path):
    box = SimpleNamespace(settings=Settings(data_root=tmp_path, history_assistance_mode="off"),
                          working_context=None, scope=None)
    request = Request(box, paired())
    before = deepcopy(request.messages)

    async def fail(_request):
        raise RuntimeError("provider unavailable")

    import pytest

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await ContextBuilderMiddleware().awrap_model_call(cast(Any, request), fail)
    assert request.messages == before


def test_revisiting_a_snapshot_appends_instead_of_replacing_old_id():
    first, _ = append_update([HumanMessage(content="task", id="u")], "host_context_update", "A")
    second, _ = append_update(first, "host_context_update", "B")
    third, added = append_update(second, "host_context_update", "A")
    assert third[:len(second)] == second and len(added) == 1
    assert added[0].id != first[-1].id
    same, added = append_update(third, "host_context_update", "A")
    assert same == third and not added


async def test_history_reference_is_retained_before_response_and_next_request(tmp_path, monkeypatch):
    async def history(*_args):
        return board_for(), {}

    monkeypatch.setattr("agent_workbench.agent.middleware.history_assistance.build_blackboard", history)
    middleware = HistoryAssistanceMiddleware()
    request = Request(box_for(tmp_path), paired())
    before = deepcopy(request.messages)
    captured = []

    async def handler(value):
        captured.append(deepcopy(value.messages))
        return ModelResponse(result=[AIMessage(content="reply", id="reply")])

    result = await middleware.awrap_model_call(cast(Any, request), handler)
    assert isinstance(result, ModelResponse)
    assert request.messages == before and isinstance(before[-1], ToolMessage)
    assert result.result[0] == captured[0][-1]
    request.messages = [*before, *result.result]
    await middleware.awrap_model_call(cast(Any, request), handler)
    assert captured[1][:len(captured[0])] == captured[0]
    assert sum(m.additional_kwargs.get("host_projection") == "host_history_reference"
               for m in captured[1]) == 1


def test_worker_total_basis_and_legacy_results_are_not_double_counted():
    common: dict[str, Any] = {"provider": "fixture", "model": "fixture", "calls": 1,
        "duration_ms": 1, "usage_status": "reported", "cache_read_tokens": 30, "cache_write_tokens": 5}
    summary = AgentTokenSummary()
    add_worker_usage(summary, WorkerUsage(**common, input_tokens=65))
    add_worker_usage(summary, WorkerUsage(**common, input_tokens=100,
                                         input_token_basis="total_including_cache"))
    assert summary.input_tokens == 200 and summary.cache_read_tokens == 60
    add_worker_usage(summary, WorkerUsage(**{**common, "cache_write_tokens": None}, input_tokens=65))
    assert summary.input_tokens == 200 and summary.unknown_input_calls == 1
    add_worker_usage(summary, WorkerUsage(**{**common, "cache_read_tokens": None}, input_tokens=100,
                                         input_token_basis="total_including_cache"))
    assert summary.input_tokens == 300 and summary.unknown_cache_calls == 1
