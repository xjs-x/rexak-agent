"""百炼 Chat Completions 参数适配，保留官方框架的模型工具循环。"""

from typing import Any

from agent_workbench.models.openai_compatible import ReasoningChatOpenAI


class BailianChatOpenAI(ReasoningChatOpenAI):
    """百炼文档使用 max_tokens；避免 SDK 的参数改名改变请求语义。"""

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload
