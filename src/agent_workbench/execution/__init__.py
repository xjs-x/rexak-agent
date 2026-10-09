"""执行层：Executor 客户端、SSH 隧道、远端事件同步。

边界（BUILD PLAN 附录 A）：
- 本包可被 `tools` 调用；`execution` **不 import** `agent`；
- SSH 断线重连使用退避，**复用持久 Executor**，不新建一套 worker/浏览器（§8.7）；
- 隧道可重建，**job 身份不变**。
"""

from __future__ import annotations

from agent_workbench.execution.client import (
    ExecutorClient,
    ExecutorConnectionError,
    ExecutorProtocolError,
    HttpExecutorClient,
)
from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable

__all__ = [
    "ExecutorClient",
    "ExecutorConnectionError",
    "ExecutorProtocolError",
    "HttpExecutorClient",
    "SshTunnel",
    "TunnelUnavailable",
]
