"""模型层：供应商适配、能力探测、请求与用量。

权威归属：`model_calls` 台账（业务表）是控制端对真实模型请求的可观测记录。
本包不持有会话历史；会话历史归图 checkpoint。

**未知能力一律标 `unknown`，不从模型名字推断**（§11.1）。
"""

from __future__ import annotations

from agent_workbench.models.provider import (
    ModelBinding,
    build_chat_model,
    resolve_provider_profile,
)

__all__ = ["ModelBinding", "build_chat_model", "resolve_provider_profile"]
