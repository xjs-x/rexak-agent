"""真实实验的离线登记契约；未知字段保留空值，不能由最终状态猜填。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUNDLE_SCHEMA_VERSION = 1


class TrialRegistration(BaseModel):
    """操作者提供的私有登记。结果项与答案不属于此契约。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trial_id: str = Field(min_length=1, max_length=128)
    environment_alias: str | None = None
    group: Literal["B0", "B0-observed", "V1"] | None = None
    repetition: int | None = Field(default=None, ge=1)
    source_commit: str | None = None
    source_tree_hash: str | None = None
    lock_hash: str | None = None
    prompt_version: str | None = None
    model_config_snapshot: dict[str, Any] | None = Field(default=None, alias="model_config")
    tool_manifest_hash: str | None = None
    environment_snapshot_id: str | None = None
    memory_snapshot_id: str | None = None
    session_id: UUID | None = None
    run_ids: list[UUID] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_kind: str | None = None
    assisted_from: datetime | None = None
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone 必须是有效的 IANA 时区") from exc
        return value

    @field_validator("started_at", "ended_at", "assisted_from")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("实验时间必须带 UTC 偏移")
        return value


class BundleFile(BaseModel):
    path: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BundleIndex(BaseModel):
    schema_version: Literal[1] = BUNDLE_SCHEMA_VERSION
    files: list[BundleFile]
