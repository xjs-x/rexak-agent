"""Agent 运行上下文（通过 `create_agent(context_schema=...)` 注入）。

**状态与运行依赖分离**（§6.1）：
- 图状态（`AgentGraphState`）只放可序列化的领域数据；
- 数据库连接、Executor 客户端、工件存储、资源锁等运行依赖放在这里，
  通过 `context_schema` 注入，工具用 `runtime.context` 访问。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_workbench.observability.model_stream import ModelStreamTracker
from agent_workbench.tools.runtime import Toolbox


@dataclass(slots=True)
class RunContext:
    """一次 run 的运行依赖集合。"""

    toolbox: Toolbox
    #: 当前 thread 的 checkpoint 命名空间信息，供恢复对账使用
    thread_id: str = ""
    #: 被关闭的工具名 -> 拒绝原因。缺能力时给**确定错误与替代能力**（§6.3）
    disabled_tools: dict[str, str] = field(default_factory=dict)
    #: 记录本次 run 中由工具触发的进度事件，供无进展判断使用
    progress_events: list[str] = field(default_factory=list)
    #: 仅用于把图流分片的时间关联到同一物理模型调用，片段结束后不持久化。
    model_stream: ModelStreamTracker = field(default_factory=ModelStreamTracker)

    def note_progress(self, signature: str) -> bool:
        """记录一个"可能算进展"的签名。

        返回 **True 表示这是新进展**。相同签名重复出现不算进展（§6.5）。
        """
        if signature in self.progress_events:
            return False
        self.progress_events.append(signature)
        return True
