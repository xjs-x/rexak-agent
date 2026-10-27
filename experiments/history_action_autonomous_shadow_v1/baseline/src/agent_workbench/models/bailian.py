"""百炼官方兼容协议适配；沿用官方框架的模型工具循环。"""

from agent_workbench.models.openai_compatible import ReasoningChatOpenAI


class BailianChatOpenAI(ReasoningChatOpenAI):
    """沿用 max_completion_tokens，完整限制思考与回答；不再回退到旧 max_tokens。"""
