"""确定性脚本模型（协议测试替身）。

用途：在没有真实模型的情况下，**按脚本**驱动 `create_agent` 的模型—工具循环，
用于验证协议、状态机、幂等与恢复行为。

**它不是效果证据**：ACCEPTANCE §14.2 明确"假模型与模拟 Executor 是协议测试替身，
不作为真实自主测试效果证明"。任何"能力提升"结论都不能引用本文件的运行结果。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """按预设顺序返回 `AIMessage` 的假模型。

    - `bind_tools()` 直接返回 self（工具 schema 不参与决策）；
    - 超出脚本长度后重复最后一条，避免测试挂死；
    - 记录每次收到的消息，便于断言"模型看到了什么"。
    """

    responses: list[AIMessage] = Field(default_factory=list)
    seen_message_counts: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-test-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_message_counts.append(len(messages))
        index = min(len(self.seen_message_counts) - 1, len(self.responses) - 1)
        if not self.responses:
            raise ValueError("ScriptedChatModel 未提供任何脚本响应")
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    """构造一个带工具调用的助手消息。"""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def final_text(text: str) -> AIMessage:
    return AIMessage(content=text)
