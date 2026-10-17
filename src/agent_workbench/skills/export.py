"""将运行真实引用的固定 Skill 依赖加入私有实验包，不推测历史生效状态。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import ArtifactRow, SkillInstallationRow, SkillVersionRow


async def collect_skill_dependencies(database: Database, payload: dict[str, Any], values: dict[str, Any]) -> None:
    catalogs = {UUID(run["config_snapshot"]["skill_catalog_artifact_id"]) for run in payload["runs"]
                if (run.get("config_snapshot") or {}).get("skill_catalog_artifact_id")}
    refs = [ref for ref in (values.get("active_skills") or {}).values() if ref]
    refs.extend(event["payload"] for event in payload["events"] if event.get("type") == "skill.activity")
    for operation in payload["operations"]:
        context = operation.get("execution_context") or {}
        refs.extend(context.get("contextual_skills") or [])
        if context.get("explicit_skill"):
            refs.append(context["explicit_skill"])
    versions = {UUID(ref["version_id"]) for ref in refs if ref.get("version_id")}
    if not catalogs and not versions:
        return
    store = ArtifactStore(database.settings)
    roles = payload.setdefault("audit_artifact_roles", {})
    artifacts = {item["id"]: item for item in payload["artifacts"]}
    missing = set(payload["missing_artifact_metadata"])
    errors: list[str] = []
    records: list[dict[str, Any]] = []

    def include(artifact: ArtifactRow | None, identifier: UUID, role: str) -> None:
        roles[str(identifier)] = role
        if artifact is None:
            missing.add(str(identifier))
            return
        artifacts[str(identifier)] = {"id": str(identifier), "sha256": artifact.sha256,
            "byte_size": artifact.byte_size, "relative_key": artifact.relative_key,
            "availability": artifact.availability, "source_operation_id": None}

    async with database.session() as db:
        for identifier in sorted(catalogs):
            artifact = await db.get(ArtifactRow, identifier)
            include(artifact, identifier, "skill_catalog")
            if artifact is None:
                errors.append(f"catalog_metadata_missing:{identifier}")
                continue
            try:
                if not store.verify(artifact.relative_key, artifact.sha256):
                    raise ValueError("catalog_hash_mismatch")
                content = json.loads(await asyncio.to_thread(store.read_bytes, artifact.relative_key))
                versions.update(UUID(item["version_id"]) for item in content)
            except (ValueError, OSError, ArtifactError) as exc:
                errors.append(f"catalog_unavailable:{identifier}:{type(exc).__name__}")
        if versions:
            rows = (await db.execute(select(SkillVersionRow).join(
                SkillInstallationRow, SkillVersionRow.installation_id == SkillInstallationRow.id
            ).where(SkillVersionRow.id.in_(versions), SkillInstallationRow.owner_id == database.settings.owner_id))).scalars().all()
            for version in rows:
                include(await db.get(ArtifactRow, version.artifact_id), version.artifact_id, "skill_package")
                records.append({"skill_id": str(version.installation_id), "version_id": str(version.id),
                    "package_hash": version.package_hash, "artifact_id": str(version.artifact_id)})
            errors.extend(f"version_missing:{identifier}" for identifier in versions - {row.id for row in rows})
    payload["artifacts"] = list(artifacts.values())
    payload["missing_artifact_metadata"] = sorted(missing)
    payload["skills"] = {"catalog_artifact_ids": [str(identifier) for identifier in sorted(catalogs)],
        "versions": records, "errors": errors, "scope": "referenced_packages_not_proof_of_effectiveness"}
