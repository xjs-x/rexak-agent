"""Configure/run the loopback Pi service used by Ultra tasks."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT, Settings, get_settings
from agent_workbench.config.ultra_worker import (
    read_worker_config,
    worker_config_path,
    write_worker_config,
)
from agent_workbench.config.worker_model import configure_worker_model
from agent_workbench.platform_compat import configure_stdio_utf8


def configure() -> None:
    settings = get_settings()
    if worker_config_path(settings).exists():
        print("Worker 配置已存在，保留原凭据。")
        return
    url = make_url(settings.database_url).set(drivername="postgresql")
    role = "rexak_ultra_" + uuid4().hex[:12]
    password, token = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    # Only newly created role is changed; app/checkpoint/model/Executor credentials stay untouched.
    with psycopg.connect(url.render_as_string(hide_password=False), connect_timeout=5) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(8795,2)")
        if worker_config_path(settings).exists():
            print("Worker 配置已存在，保留原凭据。")
            return
        present = connection.execute("SELECT to_regclass('ultra_worker.requests') IS NOT NULL").fetchone()
        if present is None or not present[0]:
            raise RuntimeError("先完成业务库迁移")
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS").format(sql.Identifier(role), sql.Literal(password)))
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(url.database or ""), sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA ultra_worker TO {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA ultra_worker TO {}").format(sql.Identifier(role)))
        # Write before DB commit; failure rolls back creation and grants.
        write_worker_config(settings, {
            "database_url": url.set(username=role, password=password).render_as_string(hide_password=False),
            "token": token,
        })
    print("已创建仅可访问 ultra_worker 台账的独立数据库角色；服务凭据已本机加密保存。")


def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure", action="store_true", help="创建独立 Worker 角色和服务凭据后退出")
    parser.add_argument("--configure-model", action="store_true", help="选择 Main 与 Worker 共用的默认模型")
    parser.add_argument("--provider", choices=["bailian", "xiaomi"], default="bailian")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--mock", action="store_true", help="显式使用确定性 Mock Provider，不读取真实模型配置")
    args = parser.parse_args()
    if args.configure:
        configure()
        return 0
    settings = get_settings()
    if args.configure_model:
        configure_worker_model(settings, args.provider, args.model)
        print("已更新 Main 与 Worker 共用的默认模型；无需保存独立 Worker 快照。")
        return 0
    argv, env = worker_process(settings, mock=args.mock)
    return subprocess.call(argv, env=env, cwd=REPO_ROOT)


def worker_process(settings: Settings, *, mock: bool = False) -> tuple[list[str], dict[str, str]]:
    """共用启动配置，让桌面入口直接托管 Node，避免遗留包装进程的子进程。"""
    config = read_worker_config(settings)
    if not config:
        raise RuntimeError("请先执行 uv run python scripts/start_ultra_worker.py --configure")
    node = shutil.which("node")
    if not node or not (REPO_ROOT / "worker/dist/server.js").exists():
        raise RuntimeError("Ultra Worker 需要 Node >=22.19 和 npm --prefix worker ci / run build")
    from urllib.parse import urlsplit

    endpoint = urlsplit(settings.ultra_worker_url)
    if endpoint.hostname != "127.0.0.1" or endpoint.scheme != "http":
        raise RuntimeError("此启动入口仅允许 127.0.0.1 HTTP Worker")
    # Deliberately do not inherit production model keys, app DB credentials or Executor token.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}}
    env.update(REXAK_WORKER_DATABASE_URL=config["database_url"],
               REXAK_WORKER_TOKEN=settings.ultra_worker_token or config["token"],
               REXAK_WORKER_PORT=str(endpoint.port or 8795))
    env["REXAK_WORKER_CONTROL_URL"] = f"http://127.0.0.1:{settings.api_port}"
    if not mock:
        # Resolve each task from Main's controller binding; legacy snapshots stay untouched.
        env["REXAK_WORKER_SHARED_MODEL"] = "1"
    return [node, str(REPO_ROOT / "worker/dist/server.js")], env


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, psycopg.Error) as exc:
        # Never print DSNs/passwords from third-party exceptions.
        print(f"Ultra Worker 启动未完成：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
