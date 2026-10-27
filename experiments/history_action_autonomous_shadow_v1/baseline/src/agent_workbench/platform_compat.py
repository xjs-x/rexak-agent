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
import os
import socket
import sys

#: 子进程必须使用的 I/O 编码。控制端的源码、日志、CLI 输出与子进程输出全部是
#: UTF-8；如果让子进程跟随 Windows 的 locale（cp936），父进程按 UTF-8 读取时会在
#: 读线程里抛 UnicodeDecodeError，表现成 `proc.stdout is None` 这种莫名其妙的错误。
CHILD_IO_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
}

_applied = False


def _bounded_socketpair(
    family: int = socket.AF_INET, type: int = socket.SOCK_STREAM, proto: int = 0
) -> tuple[socket.socket, socket.socket]:
    """Windows 回环 socketpair：连接或 accept 失败时限时退出，避免启动永久挂住。"""
    if family not in {socket.AF_INET, socket.AF_INET6} or type != socket.SOCK_STREAM or proto != 0:
        raise ValueError("仅支持 IPv4/IPv6 TCP socketpair")
    for attempt in range(3):
        listener = socket.socket(family, type, proto)
        client = socket.socket(family, type, proto)
        try:
            listener.settimeout(5)
            client.settimeout(5)
            listener.bind(("::1" if family == socket.AF_INET6 else "127.0.0.1", 0))
            listener.listen(1)
            client.connect(listener.getsockname())
            peer, _ = listener.accept()
            client.settimeout(None)
            peer.settimeout(None)
            return peer, client
        except TimeoutError:
            client.close()
            if attempt == 2:
                raise
        except BaseException:
            client.close()
            raise
        finally:
            listener.close()
    raise RuntimeError("回环 socketpair 重试失败")


def configure_local_loopback() -> None:
    """仅替换 Windows 的 Python 回环 socketpair 兜底实现。"""
    if sys.platform == "win32":
        socket.socketpair = _bounded_socketpair


def configure_event_loop_policy() -> str:
    """按平台设置事件循环策略。返回实际生效的策略名。"""
    global _applied
    configure_local_loopback()
    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        if not isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy):  # type: ignore[attr-defined]
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        _applied = True
        return "WindowsSelectorEventLoopPolicy"
    _applied = True
    return type(asyncio.get_event_loop_policy()).__name__


def configure_stdio_utf8() -> str | None:
    """把本进程的标准输出/错误流切到 UTF-8。

    默认 Windows 控制台是 cp936，直接打印中文会 `UnicodeEncodeError`；
    日志与 CLI 输出是本项目的主要诊断手段，不能因为终端 locale 就崩。
    返回实际生效的编码名（无法重配时返回 None）。
    """
    encodings: list[str] = []
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            continue
        encodings.append(str(getattr(stream, "encoding", "?")))
    return encodings[0] if encodings else None


def child_process_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：继承当前环境，但强制 UTF-8 I/O。"""
    env = dict(os.environ)
    env.update(CHILD_IO_ENV)
    if extra:
        env.update(extra)
    return env


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
