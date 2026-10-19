"""coverage profile 的 API 契约。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CoverageProfileCreate(BaseModel):
    profile_id: str = Field(min_length=1, max_length=64)
    profile_version: str = Field(min_length=1, max_length=32)
    condition_fingerprint: str = Field(min_length=16, max_length=128)
    categories: list[str] = Field(min_length=1, max_length=64)
    include_parameter_cells: bool = True
    include_anonymous: bool = True


__all__ = ["CoverageProfileCreate"]
