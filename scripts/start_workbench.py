"""本机单窗口启动：数据库、Kali Executor、API、Runner、控制台。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shlex
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Iterator
from pathlib import Path

import asyncssh
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workbench.config.deployment import (  # noqa: E402
    remote_path,
    validate_executor_connection,
)
from agent_workbench.config.settings import Settings, get_settings  # noqa: E402
from agent_workbench.platform_compat import (  # noqa: E402
    child_process_env,
    configure_local_loopback,
    configure_stdio_utf8,
)


@contextlib.contextmanager
def launcher_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("启动窗口已在运行，请使用现有窗口") from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def base_url(settings: Settings) -> str:
    host = settings.api_host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("控制端必须监听回环地址")
    return f"http://{'[' + host + ']' if ':' in host else host}:{settings.api_port}"


def port_busy(settings: Settings) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in settings.api_host else socket.AF_INET) as probe:
        probe.settimeout(1)
        return probe.connect_ex((settings.api_host, settings.api_port)) == 0


def check_existing_runner() -> None:
    if sys.platform != "win32":
        return
    # 只输出匹配 PID，避免打印其他进程的命令行或环境。
    root = str(ROOT).replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_Process | Where-Object { "
        f"$_.ExecutablePath -and $_.ExecutablePath.StartsWith('{root}', [StringComparison]::OrdinalIgnoreCase) "
        "-and $_.CommandLine -match '(?i)(workbench(?:_child\\.py|\\.exe)?|agent_workbench\\.cli).*\\brunner\\b' "
        "} | ForEach-Object { $_.ProcessId }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        check=True,
        timeout=20,
        encoding="utf-8",
        env=child_process_env(),
    )
    if result.stdout.strip():
        raise RuntimeError("已有 Runner 在运行，请先停止原 Runner，避免重复启动")


async def ensure_executor(settings: Settings) -> None:
    validate_executor_connection(settings)
    async with asyncssh.connect(
        settings.executor_host,
        port=settings.executor_port,
        username=settings.executor_user,
        client_keys=[str(settings.executor_ssh_key)],
        known_hosts=str(settings.executor_known_hosts),
        connect_timeout=15,
    ) as connection:
        async with connection.start_sftp_client() as sftp:
            remote_root = remote_path(settings.executor_install_root, str(await sftp.realpath(".")))
        remote = shlex.quote(remote_root)
        result = await connection.run(
            f"cd {remote} && .venv/bin/python executor-control.py start",
            check=False,
            timeout=45,
        )
        if result.exit_status:
            raise RuntimeError("Kali Executor 启动失败，请检查远端 executor.log")
        listener = await connection.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", settings.executor_remote_port
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.get_port()}", trust_env=False, timeout=10
            ) as client:
                response = await client.get(
                    "/capabilities", headers={"Authorization": "Bearer " + settings.executor_token}
                )
                response.raise_for_status()
                if response.json().get("executor_id") != settings.executor_id:
                    raise RuntimeError("执行端身份与本机配置不一致")
        finally:
            listener.close()


def run_check(argv: list[str], env: dict[str, str], log_path: Path, timeout: float) -> None:
    with log_path.open("wb") as log:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"启动检查未通过，详情见 {log_path}")


def stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.terminate()
        child.wait(timeout=10)
    except ProcessLookupError:
        pass


def wait_api(child: subprocess.Popen[bytes], url: str) -> None:
    deadline = time.monotonic() + 60
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("API 启动失败，请检查 data/logs/launcher/api.log")
            try:
                result = client.get(url + "/health")
                result.raise_for_status()
                info = result.json()
                if info.get("service") == "agent-workbench-api" and info["database"]["ok"]:
                    client.get(url + "/console/").raise_for_status()
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
    raise RuntimeError("API 就绪超时，请检查 data/logs/launcher/api.log")


MODEL_KEYS = {"xiaomi": "XIAOMI_API_KEY", "bailian": "DASHSCOPE_API_KEY"}


def model_environment(settings: Settings, env: dict[str, str]) -> dict[str, str]:
    """空环境值覆盖 .env，防止 API/检查进程重新读入模型密钥。"""
    return {
        **env,
        **dict.fromkeys(MODEL_KEYS.values(), ""),
        "WORKBENCH_DEFAULT_PROVIDER": settings.default_provider,
        "XIAOMI_MODEL": settings.xiaomi_model,
        "XIAOMI_BASE_URL": settings.xiaomi_base_url,
        "DASHSCOPE_MODEL": settings.dashscope_model,
        "DASHSCOPE_BASE_URL": settings.dashscope_base_url,
    }


def launch(*, api_only: bool, check_only: bool, no_browser: bool, smoke_seconds: float) -> None:
    settings = get_settings()
    settings.ensure_directories()
    log_root = settings.log_root / "launcher"
    log_root.mkdir(parents=True, exist_ok=True)
    url = base_url(settings)
    env = model_environment(settings, child_process_env())
    cli = [sys.executable, str(ROOT / "scripts/workbench_child.py")]
    with launcher_lock(log_root / "launcher.lock"):
        if port_busy(settings):
            raise RuntimeError("API 端口已有服务，请先停止原 API，避免重复启动")
        check_existing_runner()
        if not (ROOT / "web/dist/index.html").is_file():
            raise RuntimeError("控制台尚未构建，请先构建 web 前端")
        print("模型与 API Key 请在 Web 设置中配置，配置会在本机保存。", flush=True)
        print("检查数据库和 Kali 执行端……", flush=True)
        run_check(
            ["docker", "compose", "up", "-d", "--wait", "postgres"],
            env,
            log_root / "database.log",
            90,
        )
        asyncio.run(ensure_executor(settings))
        run_check([*cli, "db-init"], env, log_root / "db-init.log", 120)
        run_check([*cli, "doctor", "--skip-model"], env, log_root / "doctor.log", 90)
        print("数据库、SSH、Executor 检查通过。", flush=True)
        if check_only:
            return
        children: list[subprocess.Popen[bytes]] = []
        try:
            with contextlib.ExitStack() as stack:

                def spawn(name: str, child_env: dict[str, str]) -> subprocess.Popen[bytes]:
                    output = stack.enter_context((log_root / f"{name}.log").open("wb"))
                    child = subprocess.Popen(
                        [*cli, name],
                        cwd=ROOT,
                        env=child_env,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                        if sys.platform == "win32"
                        else 0,
                    )
                    children.append(child)
                    return child

                api = spawn("api", env)
                wait_api(api, url)
                if not api_only:
                    runner = spawn("runner", env)
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        if runner.poll() is not None:
                            raise RuntimeError(
                                "Runner 启动失败，请检查 data/logs/launcher/runner.log"
                            )
                        if "Runner 已启动" in (log_root / "runner.log").read_text(encoding="utf-8"):
                            break
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("Runner 就绪超时，请检查 data/logs/launcher/runner.log")
                print(
                    f"已启动：{url}/console/\n保留此窗口；按 Ctrl+C 停止 API 和 Runner。",
                    flush=True,
                )
                if not no_browser:
                    webbrowser.open(url + "/console/")
                deadline = time.monotonic() + smoke_seconds if smoke_seconds else None
                while deadline is None or time.monotonic() < deadline:
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("服务进程已退出，请检查 data/logs/launcher")
                    time.sleep(1)
        finally:
            for child in reversed(children):
                stop_child(child)
            print("本次启动的 API、Runner 已停止；数据库和 Kali Executor 保留运行。", flush=True)


def main() -> int:
    configure_stdio_utf8()
    configure_local_loopback()
    if sys.platform == "win32":
        # 默认 SIGBREAK 会立即结束父进程，绕过 launch 的 finally 并遗留 API/Runner。
        # 与窗口中的 Ctrl+C 一样先清理本次创建的子进程。
        signal.signal(signal.SIGBREAK, _request_stop)
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-only", action="store_true", help="只启动控制台，不执行模型任务")
    parser.add_argument("--check-only", action="store_true", help="只检查依赖，不启动 API/Runner")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-seconds", type=float, default=0)
    args = parser.parse_args()
    if args.smoke_seconds < 0 or (args.smoke_seconds and not args.api_only):
        parser.error("--smoke-seconds 仅可配合 --api-only，且必须为正数")
    try:
        launch(**vars(args))
    except KeyboardInterrupt:
        return 0
    except EOFError:
        print("输入已结束，未完成启动。", file=sys.stderr)
        return 1
    except (
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        asyncssh.Error,
        httpx.HTTPError,
    ) as exc:
        # 不打印可能包含配置或凭据的第三方异常详情。
        print(
            f"启动未完成：{exc if isinstance(exc, RuntimeError) else type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


def _request_stop(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
