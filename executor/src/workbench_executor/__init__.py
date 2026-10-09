"""执行岛常驻 Executor。

约束（BUILD_PLAN §2、§8）：
- 只监听执行岛回环地址；
- **不持有**模型 API Key 或控制端数据库凭据；
- 命令作为独立任务运行，不依附于一次 SSH channel 的生命周期；
- 本地 SQLite 台账只用于任务接收去重和重启核对，**不充当第二套项目数据库**。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
