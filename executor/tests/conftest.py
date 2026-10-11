"""执行岛测试的共享前置。

**为什么必须集中在这里**：环境变量与 `get_executor_settings()` 都是**进程级、一次性**的。
之前两个测试文件各自 `os.environ[...] = ...`，后导入的那个会覆盖前一个，
而设置对象已被 `lru_cache` 缓存 —— 结果是一个文件拿到令牌、另一个拿到不同的令牌，
报 401「管理令牌无效」。这类失败现象离根因很远，所以把前置固定在 conftest 里。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

#: 统一令牌与数据根，所有岛上测试共用
TEST_TOKEN = "executor-drill-token"
DATA_ROOT = Path(tempfile.mkdtemp(prefix="executor-drill-"))

os.environ["EXECUTOR_TOKEN"] = TEST_TOKEN
os.environ["EXECUTOR_DATA_ROOT"] = str(DATA_ROOT)
os.environ["EXECUTOR_WORKSPACE_ROOT"] = str(DATA_ROOT / "workspaces")
os.environ["EXECUTOR_NETWORK_ISOLATION"] = "unavailable"
# 用系统浏览器，不依赖 playwright install 的下载产物。
if sys.platform == "win32":
    edge_roots = [
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
    ]
    edge = next(
        (
            root / "Microsoft/Edge/Application/msedge.exe"
            for root in edge_roots
            if (root / "Microsoft/Edge/Application/msedge.exe").is_file()
        ),
        None,
    )
    if edge is not None:
        os.environ.setdefault("EXECUTOR_BROWSER_EXECUTABLE", str(edge))
else:
    os.environ.setdefault("EXECUTOR_BROWSER_EXECUTABLE", "/usr/bin/chromium")

AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(scope="session")
def client() -> Iterator[Any]:
    """整个会话共用一个应用实例（浏览器进程也因此只启一次）。"""
    import workbench_executor.api as api_module
    from fastapi.testclient import TestClient

    original_policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        # 控制端测试可能把进程全局策略改成 Selector（psycopg 所需），
        # 但 Playwright 在 Windows 启动浏览器子进程必须使用 Proactor。
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with TestClient(api_module.app) as test_client:
            yield test_client
    finally:
        asyncio.set_event_loop_policy(original_policy)


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    return dict(AUTH_HEADERS)


def target_is_up(url: str = "http://127.0.0.1:8899/") -> bool:
    import urllib.request

    try:
        # 本地 fixture 必须直连；Windows 系统代理可能把回环请求转发并返回 502，
        # 导致浏览器专项被错误标记为“目标未运行”。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False
