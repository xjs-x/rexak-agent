"""测试配置不读取日常 .env 中的供应商地址或凭据。"""

from typing import Any

from agent_workbench.config.settings import Settings


def isolated_settings(**values: Any) -> Settings:
    values["_env_file"] = None
    return Settings(**values)
