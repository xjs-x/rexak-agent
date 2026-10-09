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


def assert_supported_event_loop() -> None:
    """在事件循环内部校验当前循环是否可用。

    psycopg 的异步连接**不能**跑在 Windows 的 `ProactorEventLoop` 上。uvicorn 在
    `Server.run()` 里会把策略改回 Proactor，因此必须在**运行时**再确认一次，
    否则故障表现为"第一个数据库请求 500"，而不是可操作的启动错误。

    `workbench api` 已走程序化启动路径规避该问题；这里是为"有人直接用
    `uvicorn agent_workbench.api.app:app` 启动"这种情况准备的明确失败。
    """
    if sys.platform != "win32":
        return
    loop = asyncio.get_running_loop()
    if type(loop).__name__ == "ProactorEventLoop":
        raise RuntimeError(
            "当前事件循环是 ProactorEventLoop，psycopg 异步连接无法在其上工作。\n"
            "请改用 `uv run workbench api` 启动控制端（它会在 Selector 事件循环上运行），"
            "而不是直接调用 `uvicorn agent_workbench.api.app:app`。"
        )
