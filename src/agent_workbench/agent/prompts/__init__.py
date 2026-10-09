"""提示词与可加载方法资料。

约定（§6.3）：
- 系统提示保持**短**：角色、目标、证据等级、工具语义、持续计划、范围、停止约定、
  外部内容不可信；
- 方法资料放到**版本化 Markdown**，在需要时用只读检索工具加载，不塞进系统提示；
- 计划与任务笔记使用**结构化字段**，不要求模型输出冗长隐藏推理；
- 前端只展示简短行动理由、计划更新、事实和结果，**不依赖模型内部推理字段**。
"""

from __future__ import annotations

from agent_workbench.agent.prompts.methods import (
    METHODS_BUNDLE_VERSION,
    MethodDoc,
    MethodNotAvailable,
    list_methods,
    load_method,
)
from agent_workbench.agent.prompts.system import PROMPT_VERSION, SYSTEM_PROMPT

__all__ = [
    "METHODS_BUNDLE_VERSION",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "MethodDoc",
    "MethodNotAvailable",
    "list_methods",
    "load_method",
]
