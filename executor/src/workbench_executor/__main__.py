"""Executor 启动入口。

只监听执行岛**回环地址**；配置的令牌无效时拒绝启动。
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from workbench_executor.settings import get_executor_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workbench-executor", description="Agent Workbench 执行岛服务"
    )
    parser.add_argument("--host", default=None, help="监听地址，默认取 EXECUTOR_HOST（127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取 EXECUTOR_PORT")
    parser.add_argument("--reload", action="store_true", help="开发热重载（不用于常驻部署）")
    args = parser.parse_args(argv)

    settings = get_executor_settings()
    if not settings.token or settings.token.startswith("change-me"):
        print(
            "拒绝启动：必须设置 EXECUTOR_TOKEN（非默认值）。无鉴权的执行岛管理接口不允许上线。",
            file=sys.stderr,
        )
        return 2

    host = args.host or settings.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"拒绝启动：Executor 只允许监听回环地址，收到 {host!r}。"
            "远程访问必须通过控制端建立的 SSH 隧道。",
            file=sys.stderr,
        )
        return 2

    uvicorn.run(
        "workbench_executor.api:app",
        host=host,
        port=args.port or settings.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
