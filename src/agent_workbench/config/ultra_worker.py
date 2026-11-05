"""独立 Worker 的本机服务凭据；不改模型凭据，不随业务工件备份。"""

import base64
import json
import sys
from pathlib import Path

from agent_workbench.config.secrets import write_secret_file
from agent_workbench.config.settings import Settings


def worker_config_path(settings: Settings) -> Path:
    return settings.secret_store_path.with_name("ultra-worker.json")


def read_worker_config(settings: Settings) -> dict[str, str]:
    path = worker_config_path(settings)
    if not path.exists():
        return {}
    stored = json.loads(path.read_text(encoding="utf-8"))
    if stored.get("format") == "dpapi":
        if sys.platform != "win32":
            raise RuntimeError("Worker 服务凭据只能由原 Windows 用户解密")
        from agent_workbench.config.model_settings import _dpapi

        return json.loads(_dpapi(base64.b64decode(stored["payload"]), decrypt=True).decode("utf-8"))
    if stored.get("format") == "local" and sys.platform != "win32":
        return stored["payload"]
    raise RuntimeError("无法读取 Worker 服务凭据；禁止降级使用控制端数据库凭据")


def write_worker_config(settings: Settings, payload: dict[str, str]) -> None:
    path = worker_config_path(settings)
    if path.exists():
        raise FileExistsError("Worker 配置已存在，禁止覆盖既有服务凭据")
    if sys.platform == "win32":
        from agent_workbench.config.model_settings import _dpapi

        protected = _dpapi(json.dumps(payload).encode("utf-8"))
        write_secret_file(path, {"format": "dpapi", "payload": base64.b64encode(protected).decode("ascii")})
    else:
        write_secret_file(path, {"format": "local", "payload": payload})
