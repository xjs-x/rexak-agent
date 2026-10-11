"""MiMo 的 reasoning_content 适配；沿用官方模型调用与工具流。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI


class XiaomiChatOpenAI(ChatOpenAI):
    """保留供应商返回的思考字段，并在后续工具轮次原样回传。"""

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
        if result is not None and isinstance(result.message, AIMessageChunk) and choices:
            reasoning = (choices[0].get("delta") or {}).get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _create_chat_result(self, response: Any, generation_info: dict | None = None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get("choices") or [], strict=True):
            reasoning = (choice.get("message") or {}).get("reasoning_content")
            if isinstance(generation.message, AIMessage) and isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for message, wire in zip(messages, payload.get("messages") or [], strict=True):
            if isinstance(message, AIMessage):
                reasoning = message.additional_kwargs.get("reasoning_content")
                if isinstance(reasoning, str):
                    wire["reasoning_content"] = reasoning
        return payload
