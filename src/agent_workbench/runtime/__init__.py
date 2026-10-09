"""运行时：运行所有权、指令消费、恢复、阶段结束。

边界（§6.4）：
- Runner 外部只处理**运行所有权、等待唤醒和框架调用**，
  **不重新承担逐步推理**（那是官方 `create_agent` 的职责）；
- 同一 session 同时最多一个运行所有者；
- 达到片段工作阈值后在**没有悬空工具消息的边界**保存状态并调度下一片段。
"""

from __future__ import annotations

from agent_workbench.runtime.stopping import (
    CompletionDecision,
    FinishGate,
    no_progress_signature,
)

__all__ = ["CompletionDecision", "FinishGate", "no_progress_signature"]
