"""持久化层：数据库连接、业务表、仓储、框架初始化。

权威归属（BUILD_PLAN §4.4）：
- 业务表 = 资产/线索/发现/覆盖/任务控制/同步投影；
- 框架 checkpoint = 图 messages 与工作摘要；
- Executor 台账 = 命令/浏览器任务的真实执行状态。

本包只负责业务表与框架初始化，不做调度。
"""

from __future__ import annotations

from agent_workbench.persistence.db import (
    Database,
    get_database,
    reset_database_for_tests,
)

__all__ = ["Database", "get_database", "reset_database_for_tests"]
