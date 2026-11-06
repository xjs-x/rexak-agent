"""检查模型引用的工件确实属于当前会话且可读取。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from agent_workbench.persistence.tables import ArtifactRow
from agent_workbench.tools.runtime import Toolbox


async def verify_artifacts_exist(toolbox: Toolbox, raw_ids: list[str]) -> list[str]:
    resolved: list[str] = []
    missing: list[str] = []
    async with toolbox.db.session() as db:
        for item in raw_ids:
            candidate = item.strip()
            if not candidate:
                continue
            try:
                artifact_id = UUID(candidate)
            except ValueError:
                missing.append(candidate)
                continue
            row = (
                await db.execute(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
            ).scalar_one_or_none()
            if (
                row is None
                or row.session_id != toolbox.session_id
                or row.availability != "available"
                or not toolbox.artifacts.exists(row.relative_key)
            ):
                missing.append(candidate)
                continue
            resolved.append(str(row.id))
    if missing:
        raise ValueError(
            f"以下工件引用不可用（不存在、未同步或文件缺失）：{missing}。"
            "证据必须真实可读，请改用工具结果里返回的 artifact_id。"
        )
    return resolved
