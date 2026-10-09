"""Agent 可见工具及权限/并发包装。

原则（§6.3）：
- 首版按**能力**提供少量稳定入口，避免同时注入大量相似扫描器 schema；
- 模型能查看可用能力与缺失能力；缺少程序时给出**确定错误和替代能力**，
  不诱导它扫描整个文件系统找工具；
- 所有工具先转换成统一 `ToolResult` 再进入模型上下文，底层 stdout 不是成功标志。

**操作身份**：工具函数执行时**不允许**随机生成新的 `operation_id`（会破坏幂等）。
控制点见 `tools/operation.py`：按 `(run_id, tool_call_id)` 查既有操作，命中即复用。

## 为什么这个文件刻意不导出任何符号

具体工具模块（`tools/http.py`、`tools/task.py` …）需要 import
`agent_workbench.agent.context` 做类型标注，而 `agent.context` 又依赖
`agent_workbench.tools.runtime`。如果在本文件里急切导入子模块，就会形成
`agent.context -> tools.runtime -> tools/__init__ -> tools.http -> agent.context`
的循环导入。

因此请直接从子模块导入：

    from agent_workbench.tools.registry import build_tools, default_selection
    from agent_workbench.tools.runtime import Toolbox

这样依赖方向始终是 `agent -> tools -> execution/domain`（AGENTS.md 约定）。
"""

from __future__ import annotations
