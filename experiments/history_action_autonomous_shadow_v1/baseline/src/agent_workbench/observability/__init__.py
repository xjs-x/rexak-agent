"""可观测性：结构化日志、事件、span、费用计算。

三者不可互相冒充（§14.1）：
- **结构化日志**：故障诊断材料（structlog，含 request/trace ID）；
- **事件表**：UI 重放材料；
- **checkpoint**：Agent 恢复材料。
"""

from __future__ import annotations

from agent_workbench.observability.logging import configure_logging, get_logger
from agent_workbench.observability.spans import Span, SpanRecorder

__all__ = ["Span", "SpanRecorder", "configure_logging", "get_logger"]
