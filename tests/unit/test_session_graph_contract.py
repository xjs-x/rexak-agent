"""图语义操作与可见 Schema；不访问网络。"""

import json
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.session_graph import apply_operations, empty_graph
from agent_workbench.tools.registry import build_tools, default_selection
from agent_workbench.tools.session_graph import GRAPH_TOOLS

adapter = TypeAdapter(list[GraphOperation])


def test_review_metadata_and_bailian_tool_parsing_without_provider_request():
    from langchain_core.messages import AIMessage
    from pydantic import SecretStr

    from agent_workbench.agent.middleware.response_recovery import _review_response
    from agent_workbench.models.bailian import BailianChatOpenAI
    from tests.support.scripted_model import tool_call

    provider = BailianChatOpenAI(model="fixture", api_key=SecretStr("synthetic"), max_retries=0)
    args = {"expected_revision": 0, "operations": [], "page_id": "page:fixture"}
    result = provider._create_chat_result(
        {
            "id": "fixture",
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fixture-call",
                                "type": "function",
                                "function": {"name": "update_graph", "arguments": json.dumps(args)},
                            }
                        ],
                    },
                }
            ],
        }
    )
    assert _review_response(result.generations[0].message, GRAPH_TOOLS)[0] == "submitted_update"
    missing = tool_call("update_graph", {"expected_revision": 0, "operations": []}, "missing")
    assert _review_response(missing, GRAPH_TOOLS)[1]["error_category"] == "missing_page_id"
    broken = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "HIDDEN_SENTINEL"},
        invalid_tool_calls=[
            {
                "name": "update_graph",
                "args": "SECRET_ARGUMENT_SENTINEL",
                "id": "broken",
                "error": "invalid JSON",
            }
        ],
    )
    outcome, details = _review_response(broken, GRAPH_TOOLS)
    assert outcome == "invalid_arguments" and details["tool_names"] == ["update_graph"]
    assert "SENTINEL" not in json.dumps(details)


def test_model_schema_and_blind_defaults():
    names = {x.name for x in build_tools(default_selection())}
    assert {"get_graph", "update_graph"} <= names
    assert not names & {
        "search_memory",
        "propose_memory",
        "record_memory_usage",
        "search_methods",
        "record_observation",
        "record_lead",
        "propose_finding",
    }
    for tool in GRAPH_TOOLS:
        properties = cast(Any, tool.tool_call_schema).model_json_schema()["properties"]
        assert not set(properties) & {"runtime", "tool_call_id", "session_id", "run_id"}


def test_graph_semantics_redaction_and_revisions():
    first = adapter.validate_python(
        [
            {
                "op": "add_node",
                "graph": "information",
                "temp_id": "e",
                "data": {"label": "Authorization: Bearer secret-123"},
            },
            {
                "op": "add_node",
                "graph": "attack",
                "temp_id": "s",
                "data": {"summary": "初始状态", "entity_refs": ["e"]},
            },
            {
                "op": "add_edge",
                "graph": "attack",
                "temp_id": "n",
                "data": {
                    "from_id": "s",
                    "to_id": "s",
                    "action": "本次未见预期结果",
                    "outcome": "negative",
                    "source_refs": ["operation:one", "operation:two"],
                },
            },
        ]
    )
    original, ids = apply_operations(empty_graph(), first, "stable")
    assert "secret-123" not in json.dumps(original)
    revised = adapter.validate_python(
        [
            {
                "op": "update_node",
                "graph": "information",
                "id": ids["e"],
                "data": {"label": "条件已变化"},
            },
            {
                "op": "add_edge",
                "graph": "attack",
                "temp_id": "success",
                "data": {
                    "from_id": ids["s"],
                    "to_id": ids["s"],
                    "action": "新条件下有效",
                    "outcome": "success",
                    "source_refs": ["operation:three"],
                },
            },
        ]
    )
    result, _ = apply_operations(original, revised, "next")
    assert {x["outcome"] for x in result["attack_graph"]["edges"]} == {"negative", "success"}
    assert original["information_graph"]["nodes"][0]["label"] != "条件已变化"


@pytest.mark.parametrize("outcome", ["success", "negative", "blocked", "unknown"])
def test_all_attempt_outcomes_and_self_loop(outcome):
    operations = adapter.validate_python(
        [
            {"op": "add_node", "graph": "attack", "temp_id": "s", "data": {"summary": "测试入口"}},
            {
                "op": "add_edge",
                "graph": "attack",
                "temp_id": "a",
                "data": {
                    "from_id": "s",
                    "to_id": "s",
                    "action": "实际尝试",
                    "outcome": outcome,
                    "source_refs": ["operation:fixture"],
                },
            },
        ]
    )
    result, _ = apply_operations(empty_graph(), operations, "request")
    edge = result["attack_graph"]["edges"][0]
    assert edge["from_id"] == edge["to_id"] and edge["outcome"] == outcome
