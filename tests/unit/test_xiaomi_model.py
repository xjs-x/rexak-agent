"""供应商思考字段不能被通用适配器静默丢失。"""

import json

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from pydantic import SecretStr

from agent_workbench.config.catalog import default_provider_profiles
from agent_workbench.models.provider import ModelBinding, build_chat_model
from agent_workbench.models.xiaomi import XiaomiChatOpenAI


@pytest.mark.asyncio
async def test_actual_sdk_stream_preserves_reasoning_and_returns_it_on_next_tool_turn():
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        deltas = (
            [
                {"role": "assistant", "content": "", "reasoning_content": "验收思考分片一"},
                {"reasoning_content": "，分片二"},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-fixture",
                            "type": "function",
                            "function": {"name": "fixture", "arguments": "{}"},
                        }
                    ]
                },
            ]
            if len(requests) == 1
            else [{"role": "assistant", "content": "验收完成"}]
        )
        chunks = [
            {
                "id": "response-fixture",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in deltas
        ]
        chunks.append(
            {
                "id": "response-fixture",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            }
        )
        body = (
            "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = XiaomiChatOpenAI(
            api_key=SecretStr("fixture-only"),
            model="fixture",
            base_url="https://fixture.invalid/v1",
            http_async_client=client,
            streaming=True,
            stream_usage=True,
            max_retries=0,
            use_responses_api=False,
        )
        first = await model.ainvoke([HumanMessage(content="本地协议验收")])
        assert first.additional_kwargs["reasoning_content"] == "验收思考分片一，分片二"
        assert first.content == ""
        assert first.tool_calls[0]["name"] == "fixture"
        assert first.usage_metadata is not None
        assert first.usage_metadata.get("output_token_details", {}).get("reasoning") == 3
        await model.ainvoke(
            [
                HumanMessage(content="本地协议验收"),
                first,
                ToolMessage(content="完成", tool_call_id="call-fixture"),
            ]
        )
        assert (
            requests[1]["messages"][1]["reasoning_content"]
            == first.additional_kwargs["reasoning_content"]
        )
        assert requests[1]["messages"][1]["tool_calls"][0]["id"] == "call-fixture"
        assert len(requests) == 2


def test_nonstream_reasoning_and_default_provider_transport_settings():
    class FixtureSecrets:
        def resolve(self, secret_ref: str) -> str:
            return "fixture-only"

        def exists(self, secret_ref: str) -> bool:
            return True

    model = build_chat_model(
        ModelBinding(
            profile=default_provider_profiles()[("xiaomi", "mimo-v2.6-pro")],
            request_timeout_seconds=300,
        ),
        secret_store=FixtureSecrets(),
    )
    assert isinstance(model, XiaomiChatOpenAI)
    assert isinstance(model.request_timeout, httpx.Timeout)
    assert model.request_timeout.read == 300
    assert model.request_timeout.connect == 15
    assert model.stream_chunk_timeout == 300
    assert model.max_retries == 0
    result = model._create_chat_result(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "结果",
                        "reasoning_content": "协议测试",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )
    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "协议测试"
    chunk = model._convert_chunk_to_generation_chunk(
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        AIMessageChunk,
        None,
    )
    assert chunk is not None and isinstance(chunk.message, AIMessageChunk)
    assert chunk.message.usage_metadata is not None
    assert chunk.message.usage_metadata["total_tokens"] == 2
