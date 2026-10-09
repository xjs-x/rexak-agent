"""SSH 隧道：建立并复用端口转发，控制端通过隧道调用执行岛 HTTP 接口。

关键约束：
- 隧道只是传输层。**job 身份与隧道生命周期无关**：隧道重建后按 `job_id` 继续查询，
  不产生第二个进程（ACCEPTANCE A13）；
- 长输出下载不得占满唯一通道，导致取消/状态查询失效（§8.7）——
  Executor 客户端使用独立连接池与独立超时，输出下载不阻塞控制面请求；
- 连接失败分类为**基础设施故障**，不是工具结果（P1 清单）。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh

from agent_workbench.config.settings import Settings, get_settings


class TunnelUnavailable(RuntimeError):
    """隧道无法建立。调用方必须把它当作基础设施故障，而不是工具的阴性结论。"""


@dataclass(slots=True)
class TunnelState:
    connected: bool = False
    local_port: int | None = None
    remote_host: str = "127.0.0.1"
    remote_port: int = 0
    attempts: int = 0
    last_error: str | None = None
    _listener: object | None = None
    _conn: asyncssh.SSHClientConnection | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SshTunnel:
    """复用一条 SSH 连接 + 一个本地转发监听端口。

    控制端只与 `127.0.0.1:<local_port>` 通信；Executor 始终只监听执行岛回环。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._state = TunnelState(
            remote_host="127.0.0.1",
            remote_port=self._settings.executor_remote_port,
        )
        self._backoff_seconds = 1.0
        self._max_backoff_seconds = 30.0

    @property
    def local_base_url(self) -> str:
        if self._state.local_port is None:
            raise TunnelUnavailable("隧道尚未建立")
        return f"http://127.0.0.1:{self._state.local_port}"

    @property
    def connected(self) -> bool:
        return self._state.connected

    @property
    def last_error(self) -> str | None:
        return self._state.last_error

    async def ensure(self) -> str:
        """确保隧道可用，返回本地 base URL。并发调用只会建立一条连接。"""
        async with self._state._lock:
            if self._state.connected and self._state.local_port is not None:
                return f"http://127.0.0.1:{self._state.local_port}"
            await self._connect()
            assert self._state.local_port is not None
            return f"http://127.0.0.1:{self._state.local_port}"

    async def _connect(self) -> None:
        key_path = Path(self._settings.executor_ssh_key).expanduser()
        if not key_path.exists():
            raise TunnelUnavailable(f"SSH 私钥不存在：{key_path}")
        known_hosts = Path(self._settings.executor_known_hosts).expanduser()

        connect_kwargs: dict[str, object] = {
            "host": self._settings.executor_host,
            "port": self._settings.executor_port,
            "username": self._settings.executor_user,
            "client_keys": [str(key_path)],
            "known_hosts": str(known_hosts) if known_hosts.exists() else None,
            "connect_timeout": 15,
            # 保活，避免中间设备静默断开
            "keepalive_interval": 20,
            "keepalive_count_max": 3,
        }

        self._state.attempts += 1
        try:
            conn = await asyncssh.connect(**connect_kwargs)  # type: ignore[arg-type]
        except (OSError, asyncssh.Error) as exc:
            self._state.connected = False
            self._state.last_error = f"{type(exc).__name__}: {exc}"
            raise TunnelUnavailable(
                "无法连接执行岛 "
                f"{self._settings.executor_host}:{self._settings.executor_port}："
                f"{self._state.last_error}"
            ) from exc

        try:
            listener = await conn.forward_local_port(
                "127.0.0.1",
                self._settings.executor_local_port,
                self._state.remote_host,
                self._state.remote_port,
            )
        except (OSError, asyncssh.Error) as exc:
            # asyncssh 的 close() 是同步方法（返回 None），不做 await
            conn.close()
            self._state.connected = False
            self._state.last_error = f"{type(exc).__name__}: {exc}"
            raise TunnelUnavailable(f"无法建立端口转发：{exc}") from exc

        self._state._conn = conn
        self._state._listener = listener
        self._state.local_port = int(listener.get_port())
        self._state.connected = True
        self._state.last_error = None
        self._backoff_seconds = 1.0

    async def reconnect_with_backoff(self, *, max_attempts: int = 5) -> str:
        """带退避的重连。**不新建 worker，不重建 job 身份**。"""
        delay = self._backoff_seconds
        last_error: Exception | None = None
        for _ in range(max_attempts):
            try:
                return await self.ensure()
            except TunnelUnavailable as exc:
                last_error = exc
                await self.close()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_backoff_seconds)
        self._backoff_seconds = delay
        raise TunnelUnavailable(f"重连执行岛失败（已退避 {max_attempts} 次）：{last_error}")

    async def close(self) -> None:
        listener = self._state._listener
        conn = self._state._conn
        self._state._listener = None
        self._state._conn = None
        self._state.connected = False
        self._state.local_port = None
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.close()  # type: ignore[attr-defined]
            # asyncssh 的 listener.wait_closed() 在部分版本返回 None（不可 await），
            # 用一个事件循环让步替代，避免版本差异导致 TypeError。
            await asyncio.sleep(0)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
            with contextlib.suppress(Exception):
                await conn.wait_closed()

    async def run_remote_command(self, command: str) -> tuple[int, str, str]:
        """在岛上执行一条**诊断用**命令（doctor 使用）。

        走独立 SSH channel，不经过 Executor HTTP，也不创建 job。
        """
        await self.ensure()
        conn = self._state._conn
        if conn is None:
            raise TunnelUnavailable("SSH 连接不可用")
        result = await conn.run(command, check=False)
        return (
            int(result.exit_status or 0),
            str(result.stdout or ""),
            str(result.stderr or ""),
        )
