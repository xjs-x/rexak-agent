"""计数来自供应商序列化视图，隐藏回传字段只贡献长度和估计。"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import SecretStr

from agent_workbench.agent.context_sources import visible_message_record
from agent_workbench.agent.middleware.context_builder import _build_manifest
from agent_workbench.config.catalog import ProviderProfile
from agent_workbench.models.context_budget import (
    ContextBudgetExceeded,
    ensure_fits,
    estimate_request,
    message_inventory,
)
from agent_workbench.models.provider import request_payload
from agent_workbench.models.xiaomi import XiaomiChatOpenAI


def profile(limit=50000, reserve=1000):
    return ProviderProfile(
        provider_id="xiaomi",
        model_name="fixture",
        base_url="http://fixture.invalid/v1",
        secret_ref="",
        context_limit=limit,
        output_reserve=reserve,
    )


def test_full_payload_counts_system_tools_arguments_results_and_return_fields():
    model = XiaomiChatOpenAI(
        model="fixture", api_key=SecretStr("synthetic"), max_retries=0, use_responses_api=False
    )
    call = AIMessage(
        content="",
        id="a1",
        additional_kwargs={"reasoning_content": "hidden-sentinel" * 4000},
        tool_calls=[
            {"name": "fixture", "args": {"query": "q" * 1000}, "id": "c1", "type": "tool_call"}
        ],
    )
    messages = [
        HumanMessage(content="用户目标", id="u1"),
        call,
        ToolMessage(content="result" * 2000, tool_call_id="c1", id="t1"),
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "fixture",
                "description": "schema" * 1000,
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    system = SystemMessage(content="system" * 1000)
    estimate = estimate_request(messages, system, tools, profile(), model=model)
    assert all(
        estimate.components[key] > 1000
        for key in ("system", "messages", "tools", "provider_return_fields")
    )
    assert estimate.estimated_input_tokens == sum(estimate.components.values())
    assert estimate.allowed_input_tokens == int((50000 - 1000) * 0.9)
    assert estimate.uncertain
    assert (
        request_payload(model, [system, *messages], tools=tools)["messages"][2]["reasoning_content"]
        == call.additional_kwargs["reasoning_content"]
    )
    small = call.model_copy(update={"additional_kwargs": {"reasoning_content": "short"}})
    assert (
        estimate_request(
            [messages[0], small, messages[2]], system, tools, profile(), model=model
        ).estimated_input_tokens
        < estimate.estimated_input_tokens
    )
    inventory = message_inventory(messages)
    assert "hidden-sentinel" not in json.dumps(inventory)
    assert inventory[1]["provider_return_fields"][0]["chars"] == len(
        call.additional_kwargs["reasoning_content"]
    )
    archived = visible_message_record(call)
    assert "hidden-sentinel" not in json.dumps(archived)
    assert archived["tool_calls"][0]["id"] == "c1"


def test_over_budget_never_sends_or_leaks_content_in_error():
    estimate = estimate_request(
        [HumanMessage(content="never-echo-this" * 1000)], None, [], profile(2000, 1000)
    )
    with pytest.raises(ContextBudgetExceeded) as raised:
        ensure_fits(estimate)
    assert "never-echo-this" not in str(raised.value)
    assert estimate.allowed_input_tokens == 900


def test_manifest_does_not_claim_selected_old_messages_were_omitted():
    messages = [HumanMessage(content=f"历史 {index}", id=f"m-{index}") for index in range(30)]
    manifest = _build_manifest({}, messages, "完整系统视图")
    assert manifest["message_count"] == 30
    assert manifest["omitted"] == []
