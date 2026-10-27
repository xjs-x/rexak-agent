"""TSecBench 评测接入（外部评测入口，不属于 Agent 主循环）。

边界：
- 只通过本机 Workbench API 创建/观察 Run，不另造 Runner、不改图状态与记忆机制；
- 平台令牌与 VPN 材料只从私有配置目录读取，不入库、不入日志；
- 单题串行，默认一次只允许一个活跃题目容器。
"""

from __future__ import annotations

from agent_workbench.tsecbench.config import BenchmarkConfig, ConfigError, load_config
from agent_workbench.tsecbench.flags import FlagCandidate, extract_flag_candidates

__all__ = [
    "BenchmarkConfig",
    "ConfigError",
    "FlagCandidate",
    "extract_flag_candidates",
    "load_config",
]
