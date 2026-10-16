"""环境发现：如实报告解释器、shell、工具与工作区，不返回任何秘密。

规则（规格 §9.3）：
- 从**配置的名称与 PATH** 定位，不扫描整台机器，也不在发现时自动安装；
- 代理/CA 只报告"是否配置"，绝不返回值；
- 工作区存储能力如实标注：没有文件系统级 quota 时报告
  `accounting_only`，不把巡检说成硬隔离。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workbench_executor.settings import ExecutorSettings


def _probe_version(argv: list[str], *, timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "HOME": os.environ.get("HOME", "/tmp"),
            },
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    return raw.splitlines()[0][:200] if raw else ""


def discover_environment(settings: ExecutorSettings, *, workspace_bytes_used: int = 0) -> dict[str, Any]:
    tools: dict[str, str] = {}
    missing: dict[str, str] = {}

    def record(name: str, argv: list[str] | None = None) -> None:
        path = shutil.which(name)
        if not path:
            missing[name] = "PATH 中未找到；环境维护流程可安装"
            return
        version = _probe_version(argv or [path, "--version"])
        tools[name] = f"{path}" + (f" ({version})" if version else "")

    for name in settings.required_tools:
        record(name)

    interpreters: dict[str, str] = {}
    python_path = Path(settings.resolved_python_interpreter)
    if python_path.exists():
        interpreters["python"] = f"{python_path} ({_probe_version([str(python_path), '--version'])})"
    else:
        missing["python"] = f"配置的解释器不存在：{python_path}"
    shell_path = Path(settings.shell_path)
    if shell_path.exists():
        interpreters["shell"] = f"{shell_path} ({_probe_version([str(shell_path), '--version'])})"
    else:
        missing["shell"] = f"配置的 shell 不存在：{shell_path}"
    for name in settings.optional_interpreters:
        path = shutil.which(name)
        if path:
            interpreters[name] = path

    proxy_configured = any(
        os.environ.get(key) for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
    )
    ca_configured = bool(
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("SSL_CERT_DIR")
        or os.environ.get("REQUESTS_CA_BUNDLE")
    )
    try:
        os_release = platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except (AttributeError, OSError):
        os_release = platform.platform()

    return {
        "executor_id": settings.executor_id,
        "platform": platform.system().lower(),
        "os_release": os_release,
        "python_version": platform.python_version(),
        "shell": {"configured": str(settings.shell_path), "mode": "non-login (-c)"},
        "interpreters": interpreters,
        "tools": tools,
        "missing": missing,
        "proxy_configured": proxy_configured,
        "ca_configured": ca_configured,
        "workspace_root": str(settings.workspace_root),
        "workspace_storage_enforcement": settings.workspace_storage_enforcement,
        "workspace_bytes_used": workspace_bytes_used,
        "dependency_environment_hint": (
            "任务依赖请在本会话工作区内创建 venv/本地依赖目录（例如 "
            "`python3 -m venv .venv && ./.venv/bin/pip install ...`）；"
            "系统级软件属于环境维护，不在任务内默认安装。"
        ),
        "notes": [
            "发现过程不会自动安装任何软件。",
            "代理环境变量只表示已配置，不代表全部流量都已代理。",
            "任意命令的文件系统访问在 trusted 模式下不受 cwd 沙箱保护；"
            f"工作区存储执行方式：{settings.workspace_storage_enforcement}。",
        ],
    }


def workspace_bytes(path: Path, *, limit_entries: int = 20000) -> int:
    """有界估算工作区占用（预检与持续计量的近似值，不是硬 quota）。"""
    total = 0
    seen = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if seen > limit_entries:
                return total
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total
