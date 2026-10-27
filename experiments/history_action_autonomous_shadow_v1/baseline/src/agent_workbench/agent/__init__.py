"""Agent 层：装配、图状态、middleware、提示。

原则（§1）：
- 基础模型—工具循环使用官方 `create_agent`；只在必要的上下文、策略与结束判断处
  扩展 middleware。**不得另写一套自研 ReAct 主循环。**
- `factory.py` 是装配的**唯一入口**。
"""

from __future__ import annotations

from agent_workbench.agent.state import AgentGraphState, StateSchemaVersion, new_graph_state

__all__ = ["AgentGraphState", "StateSchemaVersion", "new_graph_state"]
