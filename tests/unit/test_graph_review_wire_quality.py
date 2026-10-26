"""真实模型适配器只接 MockTransport，证明维护工具选择进入最终出站替身。"""

import json

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import SecretStr

from agent_workbench.models.bailian import BailianChatOpenAI
from agent_workbench.tools.session_graph import GRAPH_TOOLS


async def test_graph_only_required_tool_choice_reaches_adapter_transport():
    observed = []

    def receive(request):
        assert request.url.host == "provider.invalid"
        body = json.loads(request.content)
        observed.append(body)
        assert body["tool_choice"] == "required"
        assert {t["function"]["name"] for t in body["tools"]} == {"get_graph", "update_graph"}
        return httpx.Response(
            200,
            json={
                "id": "mock-graph",
                "object": "chat.completion",
                "created": 0,
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
                                    "id": "graph-only",
                                    "type": "function",
                                    "function": {"name": "get_graph", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        model = BailianChatOpenAI(
            model="fixture",
            api_key=SecretStr("unused-offline-fixture"),
            base_url="https://provider.invalid/v1",
            http_async_client=client,
            max_retries=0,
            streaming=False,
        )
        result = await model.bind_tools(GRAPH_TOOLS, tool_choice="required").ainvoke(
            [
                SystemMessage(content="本次只维护图，不执行安全测试。"),
                HumanMessage(content="图来源数据（离线占位）。"),
            ]
        )
    assert len(observed) == 1
    assert result.tool_calls[0]["name"] == "get_graph"
