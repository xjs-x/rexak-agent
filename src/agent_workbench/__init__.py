"""Agent Workbench —— Web/API 自主黑盒测试工作台（控制端）。

包名 `agent_workbench`、CLI 名 `workbench`。执行岛代码在独立的 `workbench_executor` 包，
两者通过 HTTP 契约通信，不共享数据库或进程内状态。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
