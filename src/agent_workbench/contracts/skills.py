"""Skill 管理和模型可见数据契约。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillRead(BaseModel):
    skill_id: str
    name: str
    description: str
    source: str
    enabled: bool
    version_id: str
    package_hash: str
    label: str = ""
    diagnostics: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    default_for_projects: list[str] = Field(default_factory=list)


class SkillList(BaseModel):
    root: str = ""
    skills: list[SkillRead]
    total: int
    next_cursor: str | None = None
    errors: list[str] = Field(default_factory=list)


class SkillImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: str | None = None
    zip_base64: str | None = Field(default=None, max_length=45_000_000)


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    version_id: str | None = None
    project_ids: list[str] | None = None
    default_for_projects: list[str] | None = None


class SkillResource(BaseModel):
    path: str
    sha256: str
    byte_size: int
    text: str | None = None
    next_offset: int | None = None
    files: list[str] = Field(default_factory=list)
    files_total: int = 0
    next_file_offset: int | None = None


class SkillSessionRead(BaseModel):
    active: list[dict[str, Any]] = Field(default_factory=list)
    note: str = "活跃引用来自 checkpoint；加载不代表脚本已执行。"
