"""双图描述、官方错误回执与输入协议兼容；不访问数据库或网络。"""

import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import TypeAdapter

from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.contracts.graph_errors import GraphValidationError
from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.session_graph import apply_operations, empty_graph
from agent_workbench.tools import session_graph as graph_tools
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call


def _without_descriptions(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _without_descriptions(v)
            for k, v in value.items()
            if k not in {"description", "title"}
        }
    if isinstance(value, list):
        return [_without_descriptions(v) for v in value]
    return value


def test_visible_graph_schema_retains_frozen_input_constraints():
    """指纹来自改动前真实注册工具，忽略仅供模型阅读的描述与标题。"""
    schemas = [convert_to_openai_tool(t) for t in graph_tools.GRAPH_TOOLS]
    digest = hashlib.sha256(
        json.dumps(_without_descriptions(schemas), sort_keys=True).encode()
    ).hexdigest()
    assert digest == "1c077c0472a3deb7e9c32564861a213d4ac0b8b5af877cabf481d88e015206a5"
    for schema in schemas:
        params = schema["function"]["parameters"]["properties"]
        assert not set(params) & {"runtime", "tool_call_id", "session_id", "run_id"}
    assert "后续一轮" in graph_tools.get_graph.description
    assert "id_mapping" in graph_tools.update_graph.description


def test_complete_doc_example_keeps_graph_types_and_local_alias_lifetime():
    description = graph_tools.update_graph.description
    start = description.index('{"expected_revision":0')
    example, _ = json.JSONDecoder().raw_decode(description[start:])
    parsed = TypeAdapter(list[GraphOperation]).validate_python(example["operations"])
    document, mapping = apply_operations(empty_graph(), parsed, "documentation-example")
    assert len(document["information_graph"]["nodes"]) == 2
    assert len(document["information_graph"]["edges"]) == 1
    assert len(document["attack_graph"]["nodes"]) == 1
    assert len(document["attack_graph"]["edges"]) == 1
    state = document["attack_graph"]["nodes"][0]
    assert state["entity_refs"] == [mapping["e1"]]
    adapter = TypeAdapter(list[GraphOperation])
    next_request = [
        {
            "op": "add_node",
            "graph": "attack",
            "temp_id": "next",
            "data": {
                "summary": "后续状态",
                "entity_refs": [mapping["e1"]],
            },
        }
    ]
    apply_operations(document, adapter.validate_python(next_request), "next-example")
    next_request[0]["data"]["entity_refs"] = ["e1"]
    with pytest.raises(ValueError, match="entity_refs"):
        apply_operations(document, adapter.validate_python(next_request), "old-alias-example")


async def test_official_agent_schema_error_is_structured_and_does_not_echo_kwargs():
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "update_graph",
                {
                    "expected_revision": 0,
                    "operations": [
                        {
                            "graph": "attack",
                            "temp_id": "s",
                            "data": {"label": "UNTRUSTED_SCHEMA_INPUT_SENTINEL"},
                        }
                    ],
                },
                "invalid-graph-call",
            ),
            final_text("参数拒绝已收到"),
        ]
    )
    agent = create_agent(
        model, tools=graph_tools.GRAPH_TOOLS, middleware=[ToolDispatchMiddleware()]
    )
    result = await agent.ainvoke({"messages": [HumanMessage(content="仅检查图工具输入格式")]})
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(messages) == 1
    message = messages[0]
    assert message.status == "error" and message.tool_call_id == "invalid-graph-call"
    body = json.loads(str(message.content))
    assert body["status"] == "failed" and body["execution_certainty"] == "not_applied"
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["details"]["graph_code"] == "GRAPH_SCHEMA_INVALID"
    assert body["error"]["details"]["path"] == "operations[0].op"
    assert "UNTRUSTED_SCHEMA_INPUT_SENTINEL" not in str(message.content)


async def test_domain_rejection_exits_transaction_before_error_projection(monkeypatch):
    events = []

    @asynccontextmanager
    async def transaction():
        events.append("enter")
        try:
            yield object()
        finally:
            events.append("exit")

    async def reject(*args, **kwargs):
        raise GraphValidationError(
            "引用无效",
            reason_code="INVALID_ENTITY_REF",
            path="operations[0].data.entity_refs[0]",
            expected="当前Entity",
            actual="旧临时ID",
            recovery="使用持久化ID",
        )

    original_render = graph_tools.render_graph_failure

    def render(exc):
        assert events == ["enter", "exit"]
        events.append("project")
        return original_render(exc)

    monkeypatch.setattr(graph_tools, "commit_graph", reject)
    monkeypatch.setattr(graph_tools, "render_graph_failure", render)
    box = SimpleNamespace(
        settings=SimpleNamespace(graph_attack_enabled=True),
        db=SimpleNamespace(transaction=transaction),
        run_id="fixture-run",
    )
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box))
    coroutine = cast(Any, graph_tools.update_graph).coroutine
    result = await coroutine(
        runtime=runtime, tool_call_id="rejected-call", expected_revision=0, operations=[]
    )
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.tool_call_id == "rejected-call" and events == ["enter", "exit", "project"]
    body = json.loads(str(result.content))
    assert body["error"]["details"]["graph_code"] == "INVALID_ENTITY_REF"


async def test_success_receipt_only_adds_identity_hint_without_changing_commit(monkeypatch):
    from agent_workbench.domain import graph_sync

    events = []

    @asynccontextmanager
    async def transaction():
        yield object()

    expected = {
        "revision": 3,
        "id_mapping": {"e": "persistent-e"},
        "reviewed_seq": 12,
        "semantic_verification": "model_judgment",
    }

    async def commit(*args):
        events.append(("commit", args[2], args[3], args[-1]))
        return dict(expected)

    async def record(*args, **kwargs):
        events.append(("record", args[2]))

    async def finish(*args):
        events.append(("finish", args[2]))

    monkeypatch.setattr(graph_tools, "commit_graph", commit)
    monkeypatch.setattr(graph_sync, "record_review_outcome", record)
    monkeypatch.setattr(graph_sync, "finish_review", finish)
    box = SimpleNamespace(
        settings=SimpleNamespace(graph_attack_enabled=True),
        db=SimpleNamespace(transaction=transaction),
        run_id="fixture-run",
    )
    runtime = SimpleNamespace(context=SimpleNamespace(toolbox=box))
    coroutine = cast(Any, graph_tools.update_graph).coroutine
    body = json.loads(
        await coroutine(
            runtime=runtime,
            tool_call_id="success-call",
            expected_revision=2,
            operations=[],
            page_id="page:current",
        )
    )
    assert {key: body[key] for key in expected} == expected
    assert body["history_acknowledged"] is True and "持久化ID" in body["id_usage_hint"]
    assert events == [
        ("commit", "fixture-run:success-call", 2, "page:current"),
        ("record", "accepted_update"),
        ("finish", "updated"),
    ]
