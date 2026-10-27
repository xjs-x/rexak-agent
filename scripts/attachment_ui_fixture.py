"""独立空库 + 临时文件的前端验收服务；绝不启动 Runner 或真实模型。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import get_settings
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8


def main() -> None:
    base = get_settings()
    url = make_url(base.database_url)
    name = "workbench_attachment_ui_" + uuid4().hex
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with tempfile.TemporaryDirectory(prefix="rexak-attachment-ui-") as folder:
            root = Path(folder)
            os.environ.update(
                {
                    "WORKBENCH_DATABASE_URL": url.set(database=name).render_as_string(
                        hide_password=False
                    ),
                    "WORKBENCH_DATA_ROOT": folder,
                    "WORKBENCH_SECRET_STORE_PATH": str(root / "settings/secrets.json"),
                    "WORKBENCH_API_PORT": "8777",
                    "WORKBENCH_OWNER_ID": "attachment-ui-fixture",
                    "WORKBENCH_EXECUTOR_HOST": "127.0.0.1",
                    "WORKBENCH_EXECUTOR_PORT": "1",
                    "XIAOMI_API_KEY": "synthetic-unused",
                    "DASHSCOPE_API_KEY": "",
                    "PYTHONIOENCODING": "utf-8",
                }
            )
            get_settings.cache_clear()
            subprocess.run(
                [sys.executable, "-m", "agent_workbench.cli", "db-init"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (root / "ui-assets.txt").write_text(
                "附件界面临时验收\nasset example.invalid\n仅供只读分析，不执行或联网。\n",
                encoding="utf-8",
            )
            stop = root / "stop"
            manifest = {
                "url": "http://127.0.0.1:8777/console/",
                "file": str(root / "ui-assets.txt"),
                "stop_file": str(stop),
                "database": name,
                "runner_started": False,
            }
            evidence = Path("reports/attachment-mvp-v1/ui-fixture.json")
            evidence.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(manifest, ensure_ascii=False), flush=True)
            # api.app 在切换配置后首次导入，避免沿用日常库句柄。
            from agent_workbench.api.app import create_app

            server = uvicorn.Server(
                uvicorn.Config(create_app(), host="127.0.0.1", port=8777, log_level="warning")
            )

            def watch() -> None:
                deadline = time.monotonic() + 1800
                while not stop.exists() and time.monotonic() < deadline:
                    time.sleep(1)
                server.should_exit = True

            threading.Thread(target=watch, daemon=True).start()
            asyncio.run(server.serve())
    finally:
        # 仅删除本函数本次成功创建的随机空库，不接受外部删除目标。
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_event_loop_policy()
    main()
