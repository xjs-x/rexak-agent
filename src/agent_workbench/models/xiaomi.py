"""小米适配入口；思考字段保留由兼容协议适配器统一实现。"""

from agent_workbench.models.openai_compatible import (
    ReasoningChatOpenAI,
)
from agent_workbench.models.openai_compatible import (
    add_return_fields as add_return_fields,
)


class XiaomiChatOpenAI(ReasoningChatOpenAI):
    """保留既有小米适配入口，沿用官方 ChatOpenAI 工具流。"""
