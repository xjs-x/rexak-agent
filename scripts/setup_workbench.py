"""新成员配置与安装：每人独立 .env，保持 start-workbench.cmd 日常入口。"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workbench.config.deployment import (  # noqa: E402
    validate_executor_connection,
    validate_pip_index,
)
from agent_workbench.config.settings import get_settings  # noqa: E402
from agent_workbench.platform_compat import child_process_env, configure_stdio_utf8  # noqa: E402


def create_config(
    root: Path,
    *,
    host: str,
    user: str = "kali",
    ssh_port: int = 22,
    ssh_key: str = "~/.ssh/id_ed25519",
    known_hosts: str = "~/.ssh/known_hosts",
    pip_index_url: str = "",
) -> Path:
    if not host.strip() or not user.strip():
        raise ValueError("Kali 地址和 SSH 用户不能为空")
    validate_pip_index(pip_index_url)
    values = {
        "WORKBENCH_EXECUTOR_HOST": host.strip(),
        "WORKBENCH_EXECUTOR_USER": user.strip(),
        "WORKBENCH_EXECUTOR_PORT": str(ssh_port),
        "WORKBENCH_EXECUTOR_SSH_KEY": ssh_key,
        "WORKBENCH_EXECUTOR_KNOWN_HOSTS": known_hosts,
        "WORKBENCH_EXECUTOR_PIP_INDEX_URL": pip_index_url,
        "WORKBENCH_SESSION_SECRET": secrets.token_urlsafe(48),
        "WORKBENCH_ACCESS_TOKEN": secrets.token_urlsafe(32),
        "WORKBENCH_EXECUTOR_TOKEN": secrets.token_urlsafe(32),
    }
    if any(char in value for value in values.values() for char in "\r\n\0"):
        raise ValueError("配置不能包含换行或空字符")
    if not 1 <= ssh_port <= 65535:
        raise ValueError("SSH 端口必须在 1–65535 内")
    template = (root / ".env.example").read_text(encoding="utf-8")
    for key, value in values.items():
        template, count = re.subn(
            rf"^{key}=.*$",
            lambda _match, k=key, v=value: f"{k}={json.dumps(v, ensure_ascii=False)}",
            template,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError(f"配置模板缺少或重复字段：{key}")
    target = root / ".env"
    # O_EXCL 防止并发初始化或误操作覆盖既有令牌与连接信息。
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(template)
    return target


def run_step(argv: list[str], *, log: Path, cwd: Path) -> None:
    print(f"正在执行 {log.stem}，日志：{log}", flush=True)
    with log.open("wb") as output:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=child_process_env(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=1800,
        )
    if result.returncode:
        raise RuntimeError(f"安装步骤 {log.stem} 失败（退出码 {result.returncode}），请查看 {log}")


def install() -> None:
    if not (ROOT / ".env").is_file():
        raise RuntimeError("请先运行 configure 子命令生成自己的 .env")
    settings = get_settings()
    validate_executor_connection(settings)
    tools: dict[str, str] = {}
    for name in ("docker", "pnpm", "uv"):
        path = shutil.which(name)
        if path is None:
            raise RuntimeError(f"缺少 {name}；请先按新成员部署指南安装控制端依赖")
        tools[name] = path
    logs = settings.log_root / "setup"
    logs.mkdir(parents=True, exist_ok=True)
    build_root = settings.tmp_root / "deployment"
    build_root.mkdir(parents=True, exist_ok=True)
    metadata = tomllib.loads((ROOT / "executor/pyproject.toml").read_text(encoding="utf-8"))
    wheel = build_root / f"workbench_executor-{metadata['project']['version']}-py3-none-any.whl"
    steps = [
        ("database", [tools["docker"], "compose", "up", "-d", "--wait", "postgres"], ROOT),
        ("migration", [sys.executable, str(ROOT / "scripts/workbench_child.py"), "db-init"], ROOT),
        ("frontend-dependencies", [tools["pnpm"], "install", "--frozen-lockfile"], ROOT / "web"),
        ("frontend-build", [tools["pnpm"], "build"], ROOT / "web"),
        (
            "executor-wheel",
            [
                tools["uv"],
                "build",
                "--wheel",
                "--package",
                "workbench-executor",
                "--out-dir",
                str(build_root),
            ],
            ROOT,
        ),
        (
            "executor-install",
            [
                sys.executable,
                str(ROOT / "scripts/deploy_executor.py"),
                "--wheel",
                str(wheel),
                "--bootstrap",
                "--evidence-output",
                str(logs / "executor-deployment.json"),
            ],
            ROOT,
        ),
        ("check", [sys.executable, str(ROOT / "scripts/start_workbench.py"), "--check-only"], ROOT),
    ]
    for name, argv, cwd in steps:
        run_step(argv, log=logs / f"{name}.log", cwd=cwd)
    print("安装与连接检查完成。运行 start-workbench.cmd，在 Web 模型设置填写自己的 Key。")
    print("本次未启动 Runner，未调用模型或访问测试目标。")


def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure", help="创建独立 .env，不覆盖既有文件")
    config.add_argument("--executor-host", required=True)
    config.add_argument("--executor-user", default="kali")
    config.add_argument("--ssh-port", type=int, default=22)
    config.add_argument("--ssh-key", default="~/.ssh/id_ed25519")
    config.add_argument("--known-hosts", default="~/.ssh/known_hosts")
    config.add_argument("--pip-index-url", default="", help="可选公开 HTTPS 包源，不含凭据")
    commands.add_parser("install", help="安装本机及指定 Kali；启用可信执行，不启动 Runner")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            target = create_config(
                ROOT,
                host=args.executor_host,
                user=args.executor_user,
                ssh_port=args.ssh_port,
                ssh_key=args.ssh_key,
                known_hosts=args.known_hosts,
                pip_index_url=args.pip_index_url,
            )
            print(f"已创建 {target}；令牌独立生成且不输出。完成 SSH 配置后运行 install 子命令。")
        else:
            install()
    except FileExistsError:
        print(
            "已有 .env，保持原配置。需要更改 Kali 地址时直接编辑该文件，不重新初始化。",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # 只展示本脚本的错误说明，不回显第三方配置异常中可能含有的秘密。
        print(
            str(exc) if isinstance(exc, RuntimeError | ValueError) else type(exc).__name__,
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
