"""配置层：设置、模型目录、价格目录。

配置分层（BUILD_PLAN §16.1）：
1. 版本化配置中的非敏感默认值 —— 即本模块的 `Settings` 默认值；
2. 环境变量 / 本机密钥存储 —— 保存秘密，见 `agent_workbench.config.secrets`；
3. 数据库中的用户设置 —— 见 `agent_workbench.persistence.repositories`；
4. run 创建时的配置快照 —— 见 `agent_workbench.domain.run_config`。

优先级：运行环境变量 > 数据库用户设置 > 版本化默认值。
UI 必须显示"当前生效值来自哪一层"。
"""

from __future__ import annotations

from agent_workbench.config.catalog import (
    PRICE_CATALOG_VERSION,
    ModelPrice,
    PriceCatalog,
    ProviderProfile,
    default_provider_profiles,
)
from agent_workbench.config.settings import Settings, get_settings

__all__ = [
    "PRICE_CATALOG_VERSION",
    "ModelPrice",
    "PriceCatalog",
    "ProviderProfile",
    "Settings",
    "default_provider_profiles",
    "get_settings",
]
