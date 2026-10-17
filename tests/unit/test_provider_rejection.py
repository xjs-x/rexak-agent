"""供应商审核拒绝只能按结构化错误识别，不把普通文本或其他 400 误当成审核。"""

import json
from typing import Any, cast

import httpx
import pytest
from langchain_openai.chat_models.base import OpenAIInvalidRequestError

from agent_workbench.models.errors import content_rejection


def rejection_error(body: Any, message: str = "provider rejected") -> OpenAIInvalidRequestError:
    response = httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid/chat"))
    return OpenAIInvalidRequestError(message, response=cast(Any, response), body=body)


@pytest.mark.parametrize("wire", ["dict", "sse", "message"])
def test_content_rejection_from_json_or_stream_error(wire: str) -> None:
    data = {
        "error": {"code": "data_inspection_failed", "message": "hidden content"},
        "id": "req-fixture",
    }
    raw = "data: " + json.dumps(data)
    error = rejection_error(data if wire == "dict" else raw if wire == "sse" else None, raw)
    result = content_rejection(error)
    assert result is not None and result.code == "data_inspection_failed"
    assert result.request_id == "req-fixture"


@pytest.mark.parametrize("body", [None, {"error": {"code": "invalid_parameter"}}, "not json"])
def test_other_errors_are_not_inferred_from_the_message(body: Any) -> None:
    assert (
        content_rejection(rejection_error(body, "data_inspection_failed appears in text")) is None
    )
    assert content_rejection(ValueError("data_inspection_failed")) is None
