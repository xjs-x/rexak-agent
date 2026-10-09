"""中间件：上下文、结束判定、事件、模型调用观测。

以锁定版本的官方 hook 实现，**不手写第二套模型—工具 while 循环**（§6.2）。

| 模块 | 触发点 | 职责 |
|---|---|---|
| `context_builder` | 模型请求前 | 装配目标、当前计划、关键线索、近期结果、相关记忆与原件引用 |
| `model_observer` | 实际模型请求前后 | 请求身份、耗时、用量、价格快照、流式事件 |
| `command_guard` | 模型前/派发前 | 消费用户指令、检查暂停取消和控制版本 |
| `tool_dispatch` | 工具入口 | 参数校验、范围/能力检查、操作去重、Executor 调用 |
| `compaction` | 达到上下文阈值 | 结构化摘要、保留关键线索、记录压缩来源与旧快照 |
| `finish_gate` | 模型提出阶段结束时 | 检查挂起任务、重点线索、证据与报告；通过/继续/等待 |
"""

from __future__ import annotations

from agent_workbench.agent.middleware.compaction import build_compaction_middleware
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.finish_gate import FinishGateMiddleware
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware

__all__ = [
    "ContextBuilderMiddleware",
    "FinishGateMiddleware",
    "ModelObserverMiddleware",
    "ToolDispatchMiddleware",
    "build_compaction_middleware",
]
