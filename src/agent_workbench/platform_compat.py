"""平台兼容：事件循环策略。

Windows 的默认 `ProactorEventLoop` **不能**用于 psycopg 异步连接
（会直接抛 `InterfaceError`）。控制端在 Windows 上运行时必须显式切换到
`SelectorEventLoop`。

这不是"为了跑通而打的补丁"：控制端支持的路径就是
"Windows 控制端 + Linux 执行岛"，因此入口点必须一致地设置该策略。
所有入口（CLI 各子命令、API、Runner）都调用本函数，避免"某个入口忘了设"。
"""

from __future__ import annotations

import asyncio
import sys

_applied = False


def configure_event_loop_policy() -> str:
    """按平台设置事件循环策略。返回实际生效的策略名。"""
    global _applied
    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        if not isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy):  # type: ignore[attr-defined]
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        _applied = True
        return "WindowsSelectorEventLoopPolicy"
    _applied = True
    return type(asyncio.get_event_loop_policy()).__name__


def is_configured() -> bool:
    return _applied
