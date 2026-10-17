"""发布、检索与固定目录；正文只保存在工件，活跃引用归 checkpoint。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml
from sqlalchemy import select, text

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.skill_package import (
    MAX_PACKAGE_BYTES,
    decode_package,
    encode_package,
    package_hash,
    safe_skill_path,
    validate_files,
)
from agent_workbench.contracts.skills import SkillRead, SkillResource
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    ArtifactRow,
    RunRow,
    SessionRow,
    SkillInstallationRow,
    SkillVersionRow,
)
from agent_workbench.skills.parser import parse_skill

EXCLUDED = {".git", "node_modules", ".venv", "venv", "__pycache__"}


def read_directory(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("skill_path_denied: 目录无效或为链接")
    files: dict[str, bytes] = {}
    snapshots: dict[Path, tuple[int, int]] = {}
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        directory = Path(current)
        for name in [*dirs, *names]:
            if (directory / name).is_symlink():
                raise ValueError("skill_path_denied: 不支持源目录中的链接")
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED)
        for name in sorted(names):
            path = directory / name
            before = path.stat()
            total += before.st_size
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("skill_package_invalid: 目录过大")
            body = path.read_bytes()
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError("skill_package_invalid: 发布期间文件变化，请重新刷新")
            files[safe_skill_path(path.relative_to(root).as_posix())] = body
            snapshots[path] = (after.st_mtime_ns, after.st_size)
    # 再核对整个扫描窗口，避免前面读取的文件在读取后续附件时变化。
    current_paths: set[Path] = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in EXCLUDED]
        if any((Path(current) / name).is_symlink() for name in [*dirs, *names]):
            raise ValueError("skill_path_denied: 发布期间出现链接")
        current_paths.update(Path(current) / name for name in names)
    if current_paths != set(snapshots) or any(
        path.is_symlink() or (path.stat().st_mtime_ns, path.stat().st_size) != stamp
        for path, stamp in snapshots.items()
    ):
        raise ValueError("skill_package_invalid: 发布期间目录变化，请重新刷新")
    return validate_files(files)


class SkillService:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.db = database
        self.settings = settings or database.settings
        self.store = ArtifactStore(self.settings)

    @property
    def root(self) -> Path:
        return (self.settings.skills_root or self.settings.data_root / "skills" / "user").resolve()

    async def _artifact(self, db: Any, body: bytes, summary: str) -> ArtifactRow:
        stored = await asyncio.to_thread(self.store.write_bytes, body, media_type="application/octet-stream")
        return await ArtifactRepository(db).create(
            sha256=stored.sha256, byte_size=stored.byte_size, media_type=stored.media_type,
            relative_key=stored.relative_key, session_id=None, source_operation_id=None,
            sensitivity="internal", availability="available", captured_at=stored.captured_at,
            summary=summary,
        )

    async def publish(self, files: dict[str, bytes], source: str) -> SkillRead:
        spec = parse_skill(validate_files(files))
        digest = package_hash(files)
        archive = encode_package(files)
        # 短事务 advisory lock 防止首次发布和版本刷新并发覆盖。
        async with self.db.transaction() as db:
            lock = int.from_bytes(hashlib.sha256(f"{self.settings.owner_id}:{source}".encode()).digest()[:8], "big", signed=True)
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            row = (await db.execute(select(SkillInstallationRow).where(
                SkillInstallationRow.owner_id == self.settings.owner_id,
                SkillInstallationRow.source == source,
            ))).scalar_one_or_none()
            if row is None:
                row = SkillInstallationRow(id=uuid4(), owner_id=self.settings.owner_id,
                    source=source, name=spec["name"], enabled=True, project_ids=[], default_for_projects=[])
                db.add(row)
                await db.flush()
            version = (await db.execute(select(SkillVersionRow).where(
                SkillVersionRow.installation_id == row.id, SkillVersionRow.package_hash == digest,
            ))).scalar_one_or_none()
            if version is None:
                artifact = await self._artifact(db, archive, f"技能包 {spec['name']}@{digest}")
                version = SkillVersionRow(id=uuid4(), installation_id=row.id,
                    package_hash=digest, artifact_id=artifact.id,
                    spec={key: value for key, value in spec.items() if key != "body"})
                db.add(version)
                await db.flush()
            row.name = spec["name"]
            row.current_version_id = version.id
            return self.card(row, version)

    @staticmethod
    def card(row: SkillInstallationRow, version: SkillVersionRow) -> SkillRead:
        return SkillRead(skill_id=str(row.id), name=version.spec["name"],
            description=version.spec["description"], source=row.source, enabled=row.enabled,
            version_id=str(version.id), package_hash=version.package_hash,
            label=version.spec.get("label", ""), diagnostics=version.spec.get("diagnostics", []),
            project_ids=list(row.project_ids), default_for_projects=list(row.default_for_projects))

    async def list(self) -> list[SkillRead]:
        async with self.db.session() as db:
            rows = (await db.execute(select(SkillInstallationRow, SkillVersionRow).join(
                SkillVersionRow, SkillInstallationRow.current_version_id == SkillVersionRow.id
            ).where(SkillInstallationRow.owner_id == self.settings.owner_id)
                .order_by(SkillInstallationRow.name, SkillInstallationRow.id))).all()
            return [self.card(row, version) for row, version in rows]

    async def rescan(self) -> tuple[list[SkillRead], list[str]]:
        from agent_workbench.agent.prompts.methods import _load_all

        errors: list[str] = []
        # 旧正文无改写，只加标准元数据；search_methods 旧调用保持原行为。
        for method in _load_all().values():
            header = yaml.safe_dump({"name": method.name, "description": method.title,
                "metadata": {"version": method.sha256}}, allow_unicode=True, sort_keys=False)
            await self.publish({"SKILL.md": f"---\n{header}---\n{method.content}".encode()}, f"builtin:{method.name}")
        self.root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.root.iterdir()):
            if path.is_dir() and (path / "SKILL.md").exists():
                try:
                    files = await asyncio.to_thread(read_directory, path)
                    if parse_skill(files)["name"] != path.name:
                        raise ValueError("name 必须与目录名一致")
                    await self.publish(files, f"user:{path.name}")
                except (ValueError, OSError, yaml.YAMLError) as exc:
                    errors.append(f"{path.name}: {exc}")
        return await self.list(), errors

    async def import_directory(self, value: str) -> SkillRead:
        candidate = Path(value).expanduser().absolute()
        if any(path.is_symlink() for path in [candidate, *candidate.parents]):
            raise ValueError("skill_path_denied: 导入路径不能经过链接")
        path = candidate.resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("skill_path_denied: 只能导入已配置 Skill 根目录的子目录")
        files = await asyncio.to_thread(read_directory, path)
        if parse_skill(files)["name"] != path.name:
            raise ValueError("name 必须与目录名一致")
        return await self.publish(files, f"user:{path.relative_to(self.root).as_posix()}")

    async def files(self, skill_id: str, version_id: str | None = None,
                    project_id: str | None = None, *, enforce_enabled: bool = True) -> tuple[SkillRead, dict[str, bytes], dict[str, Any]]:
        async with self.db.session() as db:
            row = await db.get(SkillInstallationRow, UUID(skill_id))
            if row is None or row.owner_id != self.settings.owner_id:
                raise ValueError("skill_not_found")
            if enforce_enabled and not row.enabled:
                raise ValueError("skill_disabled")
            if project_id and row.project_ids and project_id not in row.project_ids:
                raise ValueError("skill_disabled: 当前项目不可见")
            version = await db.get(SkillVersionRow, UUID(version_id) if version_id else row.current_version_id)
            if version is None or version.installation_id != row.id:
                raise ValueError("skill_version_missing")
            artifact = await db.get(ArtifactRow, version.artifact_id)
            if artifact is None or artifact.availability != "available" or not self.store.verify(artifact.relative_key, artifact.sha256):
                raise ValueError("skill_hash_mismatch")
            raw = await asyncio.to_thread(self.store.read_bytes, artifact.relative_key)
            files = decode_package(raw)
            if package_hash(files) != version.package_hash:
                raise ValueError("skill_hash_mismatch")
            return self.card(row, version), files, dict(version.spec)

    async def session_files(self, session_id: UUID, skill_id: str, version_id: str | None = None
                            ) -> tuple[SkillRead, dict[str, bytes], dict[str, Any]]:
        async with self.db.session() as db:
            session = await db.get(SessionRow, session_id)
            if session is None or session.status == "deleted":
                raise ValueError("skill_not_found: 会话不存在")
            project = str(session.project_id)
        return await self.files(skill_id, version_id, project_id=project)

    async def validate_references(self, references: list[str], project_id: str) -> None:
        if len(references) > 20:
            raise ValueError("skill_context_budget_exceeded: 最多指定 20 项技能")
        for reference in set(references):
            await self.files(reference, project_id=project_id)

    async def versions(self, skill_id: str) -> list[SkillRead]:
        async with self.db.session() as db:
            row = await db.get(SkillInstallationRow, UUID(skill_id))
            if row is None or row.owner_id != self.settings.owner_id:
                raise ValueError("skill_not_found")
            versions = (await db.execute(select(SkillVersionRow).where(
                SkillVersionRow.installation_id == row.id).order_by(SkillVersionRow.created_at.desc()))).scalars().all()
            return [self.card(row, version) for version in versions]

    async def update(self, skill_id: str, values: dict[str, Any]) -> None:
        async with self.db.transaction() as db:
            row = (await db.execute(select(SkillInstallationRow).where(
                SkillInstallationRow.id == UUID(skill_id), SkillInstallationRow.owner_id == self.settings.owner_id,
            ).with_for_update())).scalar_one_or_none()
            if row is None:
                raise ValueError("skill_not_found")
            if "version_id" in values:
                version = await db.get(SkillVersionRow, UUID(values.pop("version_id")))
                if version is None or version.installation_id != row.id:
                    raise ValueError("skill_version_missing")
                row.current_version_id = version.id
            for key in ("enabled", "project_ids", "default_for_projects"):
                if key in values:
                    setattr(row, key, values[key])

    async def catalog(self, run_id: UUID) -> list[dict[str, Any]]:
        # 发布仅在首次初始化/显式刷新发生，运行中不重新读取源目录。
        if not await self.list():
            await self.rescan()
        async with self.db.transaction() as db:
            run = (await db.execute(select(RunRow).where(RunRow.id == run_id).with_for_update())).scalar_one()
            config = dict(run.config_snapshot or {})
            snapshot_id = config.get("skill_catalog_artifact_id")
            if snapshot_id:
                artifact = await db.get(ArtifactRow, UUID(snapshot_id))
                if artifact is None or not self.store.verify(artifact.relative_key, artifact.sha256):
                    raise ValueError("skill_version_missing: 运行目录快照不可用")
                return json.loads(self.store.read_bytes(artifact.relative_key))
            session = await db.get(SessionRow, run.session_id)
            if session is None:
                raise ValueError("skill_not_found: 会话不存在")
            project = str(session.project_id)
            rows = (await db.execute(select(SkillInstallationRow, SkillVersionRow).join(
                SkillVersionRow, SkillInstallationRow.current_version_id == SkillVersionRow.id
            ).where(SkillInstallationRow.owner_id == self.settings.owner_id, SkillInstallationRow.enabled.is_(True))
                .order_by(SkillInstallationRow.name, SkillInstallationRow.id))).all()
            items = [self.card(row, version).model_dump() for row, version in rows
                     if not row.project_ids or project in row.project_ids]
            artifact = await self._artifact(db, json.dumps(items, ensure_ascii=False).encode(), "Skill 运行目录快照")
            config["skill_catalog_artifact_id"] = str(artifact.id)
            config["skill_policy_version"] = 1
            run.config_snapshot = config
            return items

    @staticmethod
    def resource(files: dict[str, bytes], path: str, offset: int = 0, limit: int = 8000,
                 files_offset: int = 0) -> SkillResource:
        safe_skill_path(path)
        if path not in files:
            raise ValueError("skill_resource_not_found")
        body = files[path]
        try:
            text_value = body.decode("utf-8")
        except UnicodeDecodeError:
            text_value = None
        end = min(len(text_value or ""), offset + limit)
        names = sorted(files) if path == "SKILL.md" else []
        file_end = min(len(names), files_offset + 50)
        return SkillResource(path=path, sha256=hashlib.sha256(body).hexdigest(), byte_size=len(body),
            text=text_value[offset:end] if text_value is not None else None,
            next_offset=end if text_value is not None and end < len(text_value) else None,
            files=names[files_offset:file_end], files_total=len(names),
            next_file_offset=file_end if file_end < len(names) else None)
