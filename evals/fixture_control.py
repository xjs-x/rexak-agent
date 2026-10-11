"""评测专用管理通道；不经过 Executor 工具、模型或 Agent 工作区。"""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
from pathlib import Path
from typing import Any

import asyncssh
import httpx

from agent_workbench.config.secrets import write_secret_file
from agent_workbench.config.settings import Settings

PUBLIC_PORT = 8898
ADMIN_PORT = 8897


class FixtureControl:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.config_path = settings.data_root / "evals" / "private" / "fixture.json"
        self.conn: asyncssh.SSHClientConnection | None = None
        self.listener: Any = None
        self.http: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        known = self.settings.executor_known_hosts.expanduser()
        if not known.exists():
            raise RuntimeError("评测管理连接要求已配置 known_hosts")
        self.conn = await asyncssh.connect(
            self.settings.executor_host,
            port=self.settings.executor_port,
            username=self.settings.executor_user,
            client_keys=[str(self.settings.executor_ssh_key.expanduser())],
            known_hosts=str(known),
            connect_timeout=15,
        )

    async def open(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if config.get("executor_host") != self.settings.executor_host:
            raise RuntimeError("评测靶场配置与当前执行岛不一致")
        await self.connect()
        assert self.conn is not None
        self.listener = await self.conn.forward_local_port("127.0.0.1", 0, "127.0.0.1", ADMIN_PORT)
        self.http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.listener.get_port()}",
            headers={"Authorization": f"Bearer {config['token']}"},
            trust_env=False,
            timeout=15,
        )

    async def request(self, method: str, path: str) -> dict[str, Any]:
        if self.http is None:
            raise RuntimeError("管理通道未建立")
        response = await self.http.request(method, path)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("管理协议无效")
        return result

    async def close(self) -> None:
        if self.http:
            await self.http.aclose()
        if self.listener:
            self.listener.close()
        if self.conn:
            self.conn.close()
            await self.conn.wait_closed()
        self.http = None
        self.listener = None
        self.conn = None

    async def stop(self) -> None:
        """只结束由本配置部署的进程；PID 和脚本路径必须同时吻合。"""
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if config.get("executor_host") != self.settings.executor_host:
            raise ValueError("靶场主机不匹配")
        directory = config["remote_directory"]
        expected_parent = self.settings.remote_data_root.rstrip("/")
        if (
            not isinstance(directory, str)
            or not directory.startswith(expected_parent + "/eval-private-")
            or "/" in directory[len(expected_parent) + 1 :]
        ):
            raise ValueError("靶场目录不属于本次部署范围")
        await self.connect()
        assert self.conn is not None
        program = (
            "import os,signal; from pathlib import Path; "
            f"root=Path({directory!r}); pid=int((root/'fixture.pid').read_text()); "
            "assert pid>1; cmd=Path(f'/proc/{pid}/cmdline'); "
            "assert not cmd.exists() or str(root/'acceptance_target.py').encode() in cmd.read_bytes().split(b'\\0'); "
            "os.kill(pid,signal.SIGTERM) if cmd.exists() else None"
        )
        await self.conn.run(f"python3 -c {shlex.quote(program)}", check=True)
        self.config_path.unlink()

    async def install(self) -> dict[str, Any]:
        """只新建专用目录；已有配置时要求显式检查，拒绝覆盖运行中的靶场。"""
        if self.config_path.exists():
            raise FileExistsError("靶场配置已存在，请使用 status 检查；拒绝重复部署")
        await self.connect()
        assert self.conn is not None
        directory = f"{self.settings.remote_data_root}/eval-private-{secrets.token_hex(6)}"
        script = f"{directory}/acceptance_target.py"
        token_file = f"{directory}/admin.token"
        token = secrets.token_urlsafe(32)
        async with self.conn.start_sftp_client() as sftp:
            await sftp.mkdir(directory)
            await sftp.chmod(directory, 0o700)
            await sftp.put(str(Path(__file__).parent / "fixtures" / "acceptance_target.py"), script)
            async with sftp.open(token_file, "w") as handle:
                await handle.write(token)
            await sftp.chmod(token_file, 0o600)
        command = (
            f"nohup python3 {shlex.quote(script)} --port {PUBLIC_PORT} --admin-port {ADMIN_PORT} "
            f"--token-file {shlex.quote(token_file)} >{shlex.quote(directory + '/fixture.log')} "
            f"2>&1 < /dev/null & echo $! >{shlex.quote(directory + '/fixture.pid')}"
        )
        await self.conn.run(command, check=True)
        write_secret_file(
            self.config_path,
            {
                "token": token,
                "executor_host": self.settings.executor_host,
                "remote_directory": directory,
            },
        )
        await self.close()
        await self.open()
        for attempt in range(10):
            try:
                return await self.request("GET", "/snapshot")
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                if attempt == 9:
                    raise
                await asyncio.sleep(0.5)
        raise RuntimeError("靶场启动失败")
